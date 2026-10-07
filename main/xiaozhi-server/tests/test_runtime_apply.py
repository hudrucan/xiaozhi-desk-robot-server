"""Offline runtime maintenance/recovery, encrypted peers and local boot guards."""
import asyncio
import copy
import json
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from config.config_store import ConfigConflict, ConfigUnavailable
from core.cluster import runtime_protocol as wire
from core.cluster.runtime_apply import RuntimeApply
from core.cluster.secret_transport import SecretProvisionConfig, canonical
import runtime_apply_agent as agent

NODES = ('deskb1x', 'deskb2x', 'deskb3x')
PEERS = SecretProvisionConfig(bytes(range(32)), tuple(
    (node, f'http://10.10.10.{index+11}:8004') for index, node in enumerate(NODES)))
OP = 'a' * 32


class Reconciliation:
    def __init__(self, node):
        self.config = SimpleNamespace(secrets=PEERS)
        self.store = SimpleNamespace(bootstrap={'node_id': node,
            'google_drive': {'folder_id': 'private-fixture-folder', 'manifest_file_id': 'private-fixture-manifest'}},
            active_revision=None)
        self.source = {'desired_revision': 9, 'schema_version': 2, 'settings_scope': 'cluster'}
        self.healthy = True
        self.calls = 0
    async def reconcile(self):
        self.calls += 1
        return True


class LocalAgent:
    def __init__(self, node, events):
        self.node, self.events = node, events
        self.worker = self.core = 8
        self.state, self.operation, self.revision, self.previous = 'idle', None, None, None
        self.fingerprint = 'b' * 64
        self.fail = None
        self.pause = None
    def status(self):
        return {'node_id': self.node, 'state': self.state, 'operation': self.operation,
            'revision': self.revision, 'previous_revision': self.previous,
            'worker_revision': self.worker, 'core_revision': self.core,
            'ready': self.worker is not None and self.worker == self.core,
            'candidate_fingerprint': self.fingerprint if self.operation else None, 'code': None}
    async def __call__(self, value):
        action, op, revision = (value[key] for key in ('action', 'operation', 'revision'))
        if action in {'status', 'check'}:
            return self.status()
        self.events.append((self.node, action))
        if action == self.fail:
            raise OSError('private-fixture-key private-file-path')
        if action == 'prepare':
            if self.pause:
                await self.pause.wait()
            if self.operation and self.operation != op and self.state not in wire.TERMINAL:
                raise ConfigUnavailable()
            self.operation, self.revision, self.previous, self.state = op, revision, self.worker, 'prepared'
        else:
            if self.operation != op:
                raise ValueError('No matching transaction')
            if action == 'quiesce':
                self.worker = self.core = None
                self.state = 'quiesced'
            elif action == 'install': self.state = 'installed'
            elif action == 'worker': self.worker, self.state = revision, 'worker_started'
            elif action == 'core': self.core, self.state = revision, 'core_started'
            elif action == 'finish': self.state = 'complete'
            elif action == 'restore': self.state = 'restored'
            elif action == 'old_worker': self.worker, self.state = self.previous, 'old_worker_started'
            elif action == 'old_core': self.core, self.state = self.previous, 'old_core_started'
            elif action == 'rolled_back': self.state = 'rolled_back'
        return self.status()


class CoordinationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events, self.members, self.agents, self.services = [], {}, {}, {}
        self.offline = set()
        self.after = None
        for node in NODES:
            local = LocalAgent(node, self.events)
            service = Reconciliation(node)
            self.agents[node], self.services[node] = local, service
            member = RuntimeApply(service, local=local, exchange=self.exchange)
            self.members[node] = member
            self.addAsyncCleanup(member.stop)
        self.member = self.members[NODES[0]]
    async def exchange(self, node, envelope):
        if node in self.offline:
            raise OSError('private-fixture-key')
        peer = self.members[node]
        source, operation, payload = peer.cipher.open(canonical(envelope), 'runtime-request', node,
                                                       remote=PEERS.address(envelope['source']))
        result = await peer.receive(source, operation, payload)
        if self.after: self.after(node, payload['command']['action'])
        return canonical(peer.cipher.seal('runtime-response', node, source, operation, result))
    async def apply(self):
        await self.member.start(9)
        if self.member.task: await self.member.task
    async def test_all_candidates_and_workers_verify_before_any_core_opens(self):
        await self.apply()
        self.assertEqual(self.member.job['state'], 'complete')
        actions = [action for _, action in self.events]
        self.assertLess(max(i for i, a in enumerate(actions) if a == 'prepare'), actions.index('quiesce'))
        self.assertLess(max(i for i, a in enumerate(actions) if a == 'quiesce'), actions.index('install'))
        self.assertLess(max(i for i, a in enumerate(actions) if a == 'worker'), actions.index('core'))
        for local in self.agents.values(): self.assertEqual((local.worker, local.core), (9, 9))
        for service in self.services.values():
            self.assertIsNone(service.store.active_revision)
            self.assertGreater(service.calls, 5)
        result = await self.members[NODES[2]].status()
        self.assertEqual(result['ready_nodes'], 3)
        self.assertEqual(result['job']['state'], 'complete')
        self.assertNotIn('private-fixture', json.dumps(result))
    async def test_invalid_candidate_never_stops_a_runtime(self):
        self.agents[NODES[1]].fail = 'prepare'
        await self.apply()
        self.assertEqual(self.member.job['state'], 'failed')
        self.assertFalse(any(action == 'quiesce' for _, action in self.events))
        for local in self.agents.values(): self.assertEqual((local.worker, local.core), (8, 8))
    async def test_voice_identity_mismatch_never_stops_a_runtime(self):
        self.agents[NODES[2]].fingerprint = 'c' * 64
        await self.apply()
        self.assertEqual(self.member.job['state'], 'failed')
        self.assertFalse(any(action == 'quiesce' for _, action in self.events))
    async def test_failed_warmup_restores_all_workers_before_old_cores(self):
        self.agents[NODES[1]].fail = 'worker'
        await self.apply()
        self.assertEqual(self.member.job['state'], 'failed')
        actions = [action for _, action in self.events]
        self.assertNotIn('core', actions)
        self.assertEqual(self.member.job['node_id'], NODES[1])
        self.assertEqual(self.member.job['phase'], 'worker')
        self.assertLess(max(i for i, a in enumerate(actions) if a == 'old_worker'), actions.index('old_core'))
        for local in self.agents.values(): self.assertEqual((local.worker, local.core), (8, 8))
    async def test_cloud_drift_before_install_restores_without_new_activation(self):
        def after(node, action):
            if node == NODES[2] and action == 'quiesce': self.services[NODES[1]].source['desired_revision'] = 10
        self.after = after
        await self.apply()
        self.assertEqual(self.member.job['state'], 'failed')
        self.assertFalse(any(action == 'install' for _, action in self.events))
        for local in self.agents.values(): self.assertEqual((local.worker, local.core), (8, 8))
    async def test_competing_control_plane_cannot_apply_or_recover_a_running_job(self):
        gate = asyncio.Event()
        self.agents[NODES[0]].pause = gate
        await self.member.start(9)
        await asyncio.sleep(.01)
        result = await self.members[NODES[2]].status()
        self.assertEqual(result['job']['state'], 'running')
        self.assertFalse(result['recovery_required'])
        with self.assertRaises(ConfigUnavailable): await self.members[NODES[2]].start(9, True)
        gate.set()
        await self.member.task
    async def test_interrupted_operation_is_recoverable_from_any_control_plane(self):
        for local in self.agents.values():
            await local({'action': 'prepare', 'operation': OP, 'revision': 9})
            await local({'action': 'quiesce', 'operation': OP, 'revision': 9})
        result = await self.members[NODES[1]].status()
        self.assertTrue(result['recovery_required'])
        await self.members[NODES[1]].start(9, True)
        await self.members[NODES[1]].task
        self.assertEqual(self.members[NODES[1]].job['state'], 'rolled_back')
        for local in self.agents.values(): self.assertEqual((local.worker, local.core), (8, 8))
    async def test_offline_node_fails_before_any_mutation(self):
        self.offline.add(NODES[2])
        with self.assertRaises(ConfigUnavailable): await self.member.start(9)
        self.assertEqual(self.events, [])
    async def test_failed_rollback_does_not_open_unconfirmed_cores(self):
        self.agents[NODES[1]].fail = 'restore'
        self.agents[NODES[2]].fail = 'worker'
        await self.apply()
        self.assertEqual(self.member.job['state'], 'recovery_required')
        self.assertFalse(any(action in {'core', 'old_core'} for _, action in self.events))
        for local in self.agents.values(): self.assertIsNone(local.core)
    async def test_same_saved_revision_is_idempotent_and_no_second_restart(self):
        await self.apply()
        before = list(self.events)
        await self.apply()
        self.assertEqual(self.events, before)
        self.assertEqual(self.member.job['state'], 'complete')
    async def test_wrong_authority_or_operation_is_rejected_before_local_command(self):
        value = {'action': 'prepare', 'operation': OP, 'revision': 9, 'request_id': 'd'*32}
        with self.assertRaises(ValueError):
            await self.member.receive(NODES[1], OP, {'authority': 'wrong', 'command': value})
        with self.assertRaises(ValueError):
            await self.member.receive(NODES[1], 'c'*32, {'authority': self.member.authority, 'command': value})
        self.assertEqual(self.events, [])
    async def test_new_save_before_start_is_a_conflict(self):
        self.services[NODES[0]].source['desired_revision'] = 10
        with self.assertRaises(ConfigConflict): await self.member.start(9)
        self.assertEqual(self.events, [])


class ProtocolTests(unittest.TestCase):
    def test_revision_commands_reject_arbitrary_services_paths_and_oversize_payloads(self):
        base = {'action': 'prepare', 'operation': OP, 'revision': 9, 'request_id': 'd'*32}
        self.assertEqual(wire.command(base), base)
        for value in ({**base, 'service': 'ssh'}, {**base, 'path': '/etc/passwd'},
                      {**base, 'revision': True}, {**base, 'revision': -1},
                      {**base, 'operation': '../outside'}, {**base, 'action': 'shell'}):
            with self.assertRaises(ValueError): wire.command(value)
        with self.assertRaises(ValueError): wire.decode(b' ' * (wire.MAX_BYTES + 1))
        with self.assertRaises(ValueError): wire.decode(b'{"revision":1,"revision":2}')
    def test_public_status_rejects_extra_secret_fields(self):
        node = LocalAgent(NODES[0], []).status()
        self.assertEqual(wire.safe_node(node, NODES[0]), node)
        with self.assertRaises(ValueError): wire.safe_node({**node, 'key': 'private-fixture'}, NODES[0])


class BootGuardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.patches = [patch.object(agent, 'STATE', self.root), patch.object(agent, 'PERMITS', self.root),
                        patch.object(agent, 'read_private', lambda path, limit: Path(path).read_bytes())]
        for item in self.patches: item.start(); self.addCleanup(item.stop)
    def test_pending_partial_install_cannot_auto_start_after_power_loss(self):
        path = self.root / 'journal.json'
        for state in ('quiesced', 'installed', 'worker_started', 'core_started', 'restored'):
            path.write_text(json.dumps({'operation': OP, 'state': state}))
            with self.assertRaises(FileNotFoundError): agent.boot_guard('core')
        for state in ('complete', 'rolled_back', 'prepared'):
            path.write_text(json.dumps({'operation': OP, 'state': state}))
            self.assertEqual(agent.boot_guard('core'), 0)
    def test_only_short_lived_matching_root_permit_can_start_pending_runtime(self):
        (self.root / 'journal.json').write_text(json.dumps({'operation': OP, 'state': 'installed'}))
        permit = self.root / 'xiaozhi-worker-start.json'
        permit.write_text(json.dumps({'operation': OP, 'expires_at': time.time()+20}))
        self.assertEqual(agent.boot_guard('worker'), 0)
        for op, expires in ((OP, time.time()-1), ('b'*32, time.time()+20), (OP, time.time()+300)):
            permit.write_text(json.dumps({'operation': op, 'expires_at': expires}))
            self.assertEqual(agent.boot_guard('worker'), 1)
    def test_atomic_writer_rejects_symlink_before_chown_or_open(self):
        target = self.root / 'target'
        target.write_bytes(b'original')
        link = self.root / 'link'
        link.symlink_to(target)
        with self.assertRaises(ValueError): agent.atomic(link, b'changed')
        self.assertEqual(target.read_bytes(), b'original')


class InstallerPhaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.local = agent.Installer.__new__(agent.Installer)
        self.local.lock = asyncio.Lock()
        self.local.journal = {'operation': OP, 'revision': 9, 'previous_revision': 8, 'state': 'prepared'}
        self.local.status = AsyncMock(return_value={})
        self.local.service = AsyncMock()
        self.local.wait_ready = AsyncMock()
        self.local.require_revision = lambda revision: None
        self.local.install = lambda old=False: None
        self.local.save = lambda **fields: self.local.journal.update(fields)
        self.local.remember = lambda request: True
    async def command(self, action):
        return await self.local.dispatch({'action': action, 'operation': OP, 'revision': 9, 'request_id': uuid.uuid4().hex})
    async def test_core_cannot_start_before_local_worker_verified(self):
        with self.assertRaises(ValueError): await self.command('core')
        self.local.service.assert_not_called()
        await self.command('quiesce')
        await self.command('install')
        await self.command('worker')
        await self.command('core')
        self.assertEqual(self.local.journal['state'], 'core_started')
        self.local.wait_ready.assert_any_await('worker', 9)
        self.local.wait_ready.assert_any_await('core', 9)
    async def test_quiesce_is_journaled_before_any_service_stop(self):
        async def service(action, name): self.assertEqual(self.local.journal['state'], 'quiesced')
        self.local.service.side_effect = service
        await self.command('quiesce')
        self.assertEqual([call.args for call in self.local.service.await_args_list],
                         [('stop', 'xiaozhi-core'), ('stop', 'xiaozhi-worker')])
    async def test_failed_worker_readiness_never_records_a_successful_phase(self):
        self.local.journal['state'] = 'installed'
        self.local.wait_ready.side_effect = ValueError('private-fixture')
        with self.assertRaises(ValueError): await self.command('worker')
        self.assertEqual(self.local.journal['state'], 'installed')
        with self.assertRaises(ValueError): await self.command('core')
    async def test_different_operation_cannot_install_or_release_another_journal(self):
        for action in ('install', 'rolled_back', 'restore', 'worker', 'core'):
            with self.assertRaises(ValueError):
                await self.local.dispatch({'action': action, 'operation': 'b'*32, 'revision': 9, 'request_id': uuid.uuid4().hex})
        self.local.service.assert_not_called()


class OfflineBundleTests(unittest.TestCase):
    def setUp(self):
        from test_worker_tts_soundbank import Fixture
        from export_worker_llm_config import build_bundle as llm, write_private
        from export_worker_asr_config import build_bundle as asr
        from provision_core_soundbank import provision
        import hashlib
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.fixture = Fixture(self.root)
        config = self.fixture.config
        models = self.root / 'asr-models'
        models.mkdir()
        for key in ('encoder', 'decoder', 'joiner', 'tokens'):
            (models / key).write_bytes(key.encode())
        vad = self.root / 'vad.onnx'
        vad.write_bytes(b'fixture-vad')
        config['selected_module'].update(ASR='Local', VAD='Local', LLM='Local')
        config['ASR'] = {'Local': {'type': 'sherpa_streaming', 'model_dir': 'asr-models',
            'encoder': 'encoder', 'decoder': 'decoder', 'joiner': 'joiner', 'tokens': 'tokens'}}
        config['VAD'] = {'Local': {'type': 'silero'}}
        config['LLM'] = {'Local': {'type': 'gemini', 'model_name': 'fixture-model', 'api_key': 'fixture-local-key'}}
        self.fixture.store.secrets = SimpleNamespace(resolve=copy.deepcopy)
        baseline = {'llm': llm(self.fixture.store, 8),
            'asr': asr(self.fixture.store, vad, hashlib.sha256(vad.read_bytes()).hexdigest(), 8, self.root),
            'tts': self.fixture.export()}
        bank = self.root / 'soundbank'
        bank.mkdir()
        baseline['core'], _ = provision(baseline['tts'], self.fixture.cache, bank)
        self.paths = {}
        for key, value in baseline.items():
            path = self.root / (key + '.json')
            write_private(path, value, max_bytes=agent.LIMIT)
            self.paths[key] = path
        self.paths['env'] = self.root / 'revision.env'
        self.paths['env'].write_text('XIAOZHI_CORE_ASR_REVISION=8\n')
        self.paths['env'].chmod(0o600)
        # Disposable fixture ownership replaces only root filesystem operations;
        # the production exporters, validators and provisioning paths are real.
        self.patcher = patch.object(agent, 'read_private', lambda path, limit=agent.LIMIT: Path(path).read_bytes())
        self.patcher.start(); self.addCleanup(self.patcher.stop)
        def atomic(path, content, gid=0, mode=0o600):
            Path(path).write_bytes(content); Path(path).chmod(mode)
        self.atomic = patch.object(agent, 'atomic', atomic)
        self.atomic.start(); self.addCleanup(self.atomic.stop)
        self.local = agent.Installer.__new__(agent.Installer)
        self.local.config = {'node_id': NODES[0], 'nodes': list(NODES)}
        self.local.root = self.root / 'transactions'; self.local.root.mkdir()
        self.local.source = self.root
        self.local.bundles = self.paths
        self.local.worker_gid = self.local.core_gid = os.getgid()
        self.local.journal = None
        self.snapshot = {'payload': {'manifest': {'revision': 9}, 'object': {'schema_version': 2,
            'layers': {key: {} for key in ('global', 'environments', 'roles', 'cluster', 'nodes')}}}}
        self.fixture.store._read_cache = lambda _: self.snapshot
        self.local.store = lambda: self.fixture.store
        self.fixture.ready(9)
        self.config = config
        self.before = {key: path.read_bytes() for key, path in self.paths.items()}
    def test_new_revision_uses_local_validated_exports_and_restores_exact_old_bytes(self):
        self.config['TTS']['Local']['speed'] = 1.1
        self.config['LLM']['Local']['model_name'] = 'fixture-new-model'
        self.local.prepare(OP, 9)
        self.assertEqual(self.local.journal['state'], 'prepared')
        self.assertEqual({key: path.read_bytes() for key, path in self.paths.items()}, self.before)
        self.local.install()
        for key in ('llm', 'asr', 'tts', 'core'):
            self.assertEqual(json.loads(self.paths[key].read_bytes())['revision'], 9)
        self.assertEqual(self.paths['env'].read_text(), 'XIAOZHI_CORE_ASR_REVISION=9\n')
        self.assertEqual(json.loads(self.paths['tts'].read_bytes())['options']['speed'], 1.1)
        self.local.install(True)
        self.assertEqual({key: path.read_bytes() for key, path in self.paths.items()}, self.before)
    def test_missing_soundbank_bytes_fails_before_any_live_bundle_changes(self):
        self.fixture.asset.unlink()
        with self.assertRaises(OSError): self.local.prepare(OP, 9)
        self.assertIsNone(self.local.journal)
        self.assertEqual({key: path.read_bytes() for key, path in self.paths.items()}, self.before)
    def test_model_path_changes_fail_before_opening_unprovisioned_assets(self):
        self.config['TTS']['Local']['model_dir'] = 'data/node-secrets'
        with self.assertRaises(ValueError): self.local.prepare(OP, 9)
        self.assertIsNone(self.local.journal)
    def test_missing_key_or_corrupt_vad_blocks_prepare_and_preserves_bundles(self):
        self.config['LLM']['Local']['api_key'] = ''
        with self.assertRaises(ValueError): self.local.prepare(OP, 9)
        self.assertIsNone(self.local.journal)
        self.config['LLM']['Local']['api_key'] = 'fixture-local-key'
        (self.root / 'vad.onnx').write_bytes(b'changed')
        with self.assertRaises(ValueError): self.local.prepare(OP, 9)
        self.assertEqual({key: path.read_bytes() for key, path in self.paths.items()}, self.before)
    def test_durable_request_ids_ignore_replays_across_agent_restart(self):
        self.local.requests = {}
        self.assertTrue(self.local.remember(OP))
        self.assertFalse(self.local.remember(OP))
        self.local.requests = json.loads((self.local.root / 'requests.json').read_bytes())
        self.assertFalse(self.local.remember(OP))


if __name__ == '__main__':
    unittest.main()
