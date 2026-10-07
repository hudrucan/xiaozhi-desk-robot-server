"""Symmetric Settings coordinator for an explicit all-node maintenance apply."""
import asyncio
import hashlib
import json
import logging
import uuid

from aiohttp import ClientSession, ClientTimeout

from config.config_store import ConfigConflict, ConfigUnavailable
from . import runtime_protocol as wire
from .secret_transport import SecretCipher, canonical

LOGGER = logging.getLogger('xiaozhi.runtime_apply')


async def local_exchange(value):
    reader, writer = await asyncio.open_unix_connection('/run/xiaozhi-runtime-apply/apply.sock',
                                                       limit=wire.MAX_BYTES + 1)
    try:
        writer.write(canonical(wire.command(value)) + b'\n')
        await writer.drain()
        result = wire.decode(await asyncio.wait_for(reader.readline(), 300))
        if not isinstance(result, dict) or result.get('ok') is not True or set(result) != {'ok', 'node'}:
            raise ConfigUnavailable('Local runtime agent did not confirm')
        return result['node']
    finally:
        writer.close()
        await writer.wait_closed()


class RuntimeApply:
    def __init__(self, reconciliation, *, local=None, exchange=None):
        self.reconciliation = reconciliation
        self.config = reconciliation.config.secrets
        self.node = reconciliation.store.bootstrap['node_id']
        drive = reconciliation.store.bootstrap['google_drive']
        self.authority = hashlib.sha256(canonical({
            'folder': drive['folder_id'], 'manifest': drive['manifest_file_id']})).hexdigest()
        self.cipher = SecretCipher(self.config)
        self.local = local or local_exchange
        self.exchange = exchange
        self.session = None
        self.task = None
        self.stopping = False
        self.job = {'state': 'idle', 'phase': None, 'node_id': None, 'revision': None,
                    'operation': None, 'code': None}
        self.status_lock = asyncio.Lock()

    async def _http(self, node, envelope):
        if self.session is None:
            self.session = ClientSession(trust_env=False, timeout=ClientTimeout(total=305))
        async with self.session.post(self.config.endpoint(node) + '/internal/settings/runtime',
                                     json=envelope, allow_redirects=False) as response:
            data = bytearray()
            async for chunk in response.content.iter_chunked(4096):
                data.extend(chunk)
                if len(data) > 16384:
                    raise ValueError('Runtime peer response exceeds limit')
            if response.status != 200:
                raise ConfigUnavailable('Runtime peer did not confirm')
            return bytes(data)

    async def peer(self, node, action, operation, revision):
        value = wire.command({'action': action, 'operation': operation, 'revision': revision,
                              'request_id': uuid.uuid4().hex})
        if node == self.node:
            result = await self.receive(self.node, operation, {'authority': self.authority, 'command': value})
        else:
            envelope = self.cipher.seal('runtime-request', self.node, node, operation,
                                        {'authority': self.authority, 'command': value})
            response = await (self.exchange or self._http)(node, envelope)
            _, _, result = self.cipher.open(response, 'runtime-response', self.node,
                                            source=node, operation=operation)
        if action == 'job':
            return wire.safe_job(result)
        result = wire.safe_node(result, node)
        if action not in {'status', 'check'} and (result['operation'] != operation or result['revision'] != revision):
            raise ConfigUnavailable('Runtime acknowledgement identity differs')
        return result

    async def receive(self, source, operation, payload):
        if (not isinstance(payload, dict) or set(payload) != {'authority', 'command'}
                or payload['authority'] != self.authority):
            raise ValueError('Runtime Cloud authority differs')
        command = wire.command(payload['command'])
        if command['operation'] != operation:
            raise ValueError('Runtime operation differs')
        if command['action'] == 'job':
            return wire.safe_job(self.job)
        # Each mutation is checked against live Cloud before local files/services
        # change. Recovery deliberately uses the old immutable journal instead.
        if command['action'] in {'check', 'prepare', 'install', 'worker', 'core'}:
            await self.guard(command['revision'])
        return wire.safe_node(await self.local(command), self.node)

    async def guard(self, revision):
        if not await self.reconciliation.reconcile():
            raise ConfigUnavailable('Cloud revision could not be confirmed')
        source = self.reconciliation.source
        if (not self.reconciliation.healthy or source.get('schema_version') != 2
                or source.get('settings_scope') != 'cluster'
                or source.get('desired_revision') != revision):
            raise ConfigConflict('Cloud changed during runtime apply')

    async def status(self):
        operation = uuid.uuid4().hex
        revision = self.reconciliation.source.get('desired_revision') or 1
        async def one(node):
            try:
                return await asyncio.wait_for(self.peer(node, 'status', operation, revision), 5)
            except Exception:
                return {'node_id': node, 'state': 'unavailable', 'ready': False,
                        'worker_revision': None, 'core_revision': None}
        async def job(node):
            try:
                return await asyncio.wait_for(self.peer(node, 'job', operation, revision), 3)
            except Exception:
                return None
        nodes, jobs = await asyncio.gather(
            asyncio.gather(*(one(node) for node, _ in self.config.nodes)),
            asyncio.gather(*(job(node) for node, _ in self.config.nodes)))
        running = [job for job in jobs if job and job['state'] == 'running']
        current = running[0] if running else max((job for job in jobs if job),
            key=lambda value: (value['revision'] or 0, value['state'] in {'failed', 'recovery_required'}),
            default=dict(self.job))
        pending = [node for node in nodes if node['state'] not in wire.TERMINAL | {'idle', 'unavailable'}]
        ready = sum(node.get('ready') and node['worker_revision'] == node['core_revision'] == revision for node in nodes)
        return {'protocol': wire.PROTOCOL, 'desired_revision': revision, 'ready_nodes': ready,
                'expected_nodes': 3, 'nodes': nodes, 'job': current,
                'recovery_required': bool(pending) and not running, 'scope': ['ASR', 'VAD', 'LLM', 'TTS', 'Soundbank']}

    async def start(self, revision, recover=False):
        if type(revision) is not int or not 1 <= revision < 2**53 or type(recover) is not bool:
            raise ValueError('Invalid runtime revision')
        if self.stopping or (self.task and not self.task.done()):
            raise ConfigUnavailable('Runtime apply is already running')
        async with self.status_lock:
            if self.task and not self.task.done():
                raise ConfigUnavailable('Runtime apply is already running')
            await self.guard(revision)
            status = await self.status()
            if status['job']['state'] == 'running':
                raise ConfigUnavailable('Another control plane is applying runtime')
            if any(node['state'] == 'unavailable' for node in status['nodes']):
                raise ConfigUnavailable('All three runtime agents must be available')
            pending = [node for node in status['nodes'] if node['state'] not in wire.TERMINAL | {'idle'}]
            if pending:
                identities = {(node['operation'], node['revision']) for node in pending}
                if not recover or len(identities) != 1:
                    raise ConfigUnavailable('Recover the previous runtime operation first')
                operation, target = identities.pop()
                # A later Save cannot turn an interrupted apply into a new one.
                # Recovery restores the exact old bundles on all prepared nodes.
                members = [node['node_id'] for node in status['nodes'] if node.get('operation') == operation]
                self.job.update(state='running', phase='recovering', node_id=None,
                                revision=target, operation=operation, code=None)
                self.task = asyncio.create_task(self._recover(members, operation, target))
            elif recover:
                raise ValueError('There is no interrupted operation to recover')
            elif status['ready_nodes'] == 3:
                self.job.update(state='complete', phase='verified', revision=revision, node_id=None,
                                operation=None, code=None)
            else:
                operation = uuid.uuid4().hex
                self.job.update(state='running', phase='preparing', node_id=None,
                                revision=revision, operation=operation, code=None)
                self.task = asyncio.create_task(self._run(operation, revision))
        return dict(self.job)

    async def phase(self, nodes, action, operation, revision, *, guard=True):
        replies = []
        for node in nodes:
            self.job.update(phase=action, node_id=node)
            if guard:
                # Check every control plane, including nodes already processed.
                await asyncio.gather(*(self.peer(n, 'check', operation, revision) for n, _ in self.config.nodes))
            result = await self.peer(node, action, operation, revision)
            replies.append(result)
        return replies

    async def _rollback(self, nodes, operation, revision, *, prepared_only=False):
        if not nodes:
            return
        if not prepared_only:
            # If any node is unreachable, do not start old cores against an
            # unconfirmed mix of old/new workers. Leave a recoverable journal.
            await self.phase(nodes, 'quiesce', operation, revision, guard=False)
            await self.phase(nodes, 'restore', operation, revision, guard=False)
            await self.phase(nodes, 'old_worker', operation, revision, guard=False)
            await self.phase(nodes, 'old_core', operation, revision, guard=False)
        await self.phase(nodes, 'rolled_back', operation, revision, guard=False)

    async def _recover(self, nodes, operation, revision):
        try:
            await self._rollback(nodes, operation, revision)
            self.job.update(state='rolled_back', phase='verified', node_id=None, code=None)
        except Exception:
            self.job.update(state='recovery_required', code='runtime_recovery_required')
            LOGGER.warning('Runtime recovery incomplete; keep conversation cores stopped until all nodes confirm')

    async def _run(self, operation, revision):
        nodes = [node for node, _ in self.config.nodes]
        prepared = []
        mutated = False
        try:
            candidates = []
            for node in nodes:
                self.job.update(phase='prepare', node_id=node)
                await self.guard(revision)
                # Sorted acquisition makes competing VIP/control-plane callers
                # contend on the same first journal; no permanent coordinator.
                result = await self.peer(node, 'prepare', operation, revision)
                prepared.append(node)
                candidates.append(result['candidate_fingerprint'])
            if len(set(candidates)) != 1 or candidates[0] is None:
                raise ValueError('TTS voice identities differ across nodes')
            # No runtime has changed before every candidate validates.
            mutated = True
            await self.phase(nodes, 'quiesce', operation, revision)
            await self.phase(nodes, 'install', operation, revision)
            await self.phase(nodes, 'worker', operation, revision)
            await self.phase(nodes, 'core', operation, revision)
            replies = await self.phase(nodes, 'finish', operation, revision)
            if any(not item['ready'] or item['worker_revision'] != item['core_revision']
                   or item['core_revision'] != revision for item in replies):
                raise ValueError('Final runtime revisions differ')
            self.job.update(state='complete', phase='verified', node_id=None, code=None)
        except Exception:
            # A lost prepare response may still have acquired a durable lock.
            # Discover only this operation; never release another caller's lock.
            for node in nodes:
                try:
                    result = await self.peer(node, 'status', operation, revision)
                    if result.get('operation') == operation and node not in prepared:
                        prepared.append(node)
                except Exception:
                    pass
            try:
                await self._rollback(prepared, operation, revision, prepared_only=not mutated)
                self.job.update(state='failed', phase='rolled_back', node_id=None, code='runtime_apply_failed')
            except Exception:
                self.job.update(state='recovery_required', code='runtime_recovery_required')
            LOGGER.warning('Runtime apply did not complete; inspect safe per-node apply status')

    async def stop(self):
        self.stopping = True
        # Own the background operation even if the browser disconnects or the
        # HTTP process drains. Root journals cover hard process/power loss.
        if self.task and not self.task.done():
            await asyncio.shield(self.task)
        if self.session is not None:
            await self.session.close()
