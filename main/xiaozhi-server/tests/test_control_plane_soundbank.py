"""Cluster Soundbank authoring: fake workers/Drive, disposable audio and HTTP."""
import asyncio
import copy
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import yaml
from aiohttp.test_utils import TestClient, TestServer

from config.control_plane import ControlPlaneConfig
from core.cluster.secret_transport import SecretProvisionConfig
from core.control_plane import create_app, RECONCILIATION_KEY
from core.cluster.soundbank_authoring import AudioCompiler, SoundbankAuthoring
from core.cluster.tts_client import TTSPool
from core.cluster.worker_rpc import WorkerRpcError
from core.soundbank import SoundbankError
from test_control_plane import FakeClient, make_fixture


class SoundbankTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture, self.store, config = make_fixture()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.defaults.update(xiaozhi={'audio_params': {'sample_rate': 16000}},
            TTS={'TestTTS': {'type': 'sherpa'}},
            selected_module={'LLM': 'Test', 'TTS': 'TestTTS'})
        self.fixture.default_path.write_text(yaml.safe_dump(self.fixture.defaults))
        peers = SecretProvisionConfig(b'k' * 32, (('test-node', 'http://127.0.0.1:8004'),
            ('test-b', 'http://10.10.10.12:8004'), ('test-c', 'http://10.10.10.13:8004')))
        self.config = ControlPlaneConfig(config.nats, secrets=peers)
        self.pcm = AsyncMock(return_value=SimpleNamespace(pcm=b'\0\0' * 3200,
                                                         sample_rate=16000))
        self.normalize = patch.object(AudioCompiler, '_normalize_wav',
            side_effect=lambda source, destination, rate: shutil.copyfile(source, destination))
        self.normalize.start()
        self.addCleanup(self.normalize.stop)
        app = create_app(self.store, self.config, client_factory=FakeClient, soundbank_generate=self.pcm)
        self.service = app[RECONCILIATION_KEY]
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def settle(self, response):
        if response.status != 202:
            return response.status, await response.json()
        job = await response.json()
        self.assertRegex(job['poll_url'], r'^/api/settings/soundbank/jobs/[0-9a-f]{32}$')
        for _ in range(100):
            await asyncio.sleep(.01)
            value = await (await self.client.get(job['poll_url'])).json()
            if value['state'] == 'complete':
                return 200, value['result']
            if value['state'] == 'failed':
                return value['status'], value
        self.fail('Soundbank job did not complete')

    async def generate(self, text='A synthetic test response'):
        r = await self.client.post('/api/settings/soundbank/generate',
            json={'text': text, 'title': 'Synthetic phrase', 'base_revision': 1})
        self.assertEqual(r.status, 202, await r.text())
        status, result = await self.settle(r)
        self.assertEqual(status, 200, result)
        return result

    async def test_generate_preview_save_rename_delete_keeps_runtime_files_untouched(self):
        original = (self.fixture.directory / 'soundbank/hello.wav').read_bytes()
        value = await self.generate()
        entry = value['entry']
        self.assertIn('cloud', entry)
        self.assertIn('cloud', entry['optimized'])
        self.assertEqual(self.store.revision_unlocked(), 1)
        self.assertIsNone(self.store.runtime_snapshot)
        self.assertEqual(list(self.service.authoring.workspace.iterdir()), [])
        for filename, token in value['previews'].items():
            response = await self.client.get('/api/settings/soundbank/audio/' + filename,
                                            params={'preview_token': token})
            self.assertEqual(response.status, 200, await response.text() if response.status != 200 else '')
            self.assertEqual(response.content_type, 'audio/wav')
            self.assertTrue((await response.read()).startswith(b'RIFF'))
        save = await self.client.put('/api/settings', json={'base_revision': 1,
            'config': {'static_soundbank': {'entries': {'Synthetic phrase': entry}}}})
        self.assertEqual(save.status, 200, await save.text())
        self.assertEqual((await self.client.get('/api/settings/soundbank/audio/' + entry['file'])).status, 200)
        rename = await self.client.put('/api/settings', json={'base_revision': 2,
            'config': {'static_soundbank': {'entries': {'Renamed phrase': entry}}}})
        self.assertEqual(rename.status, 200, await rename.text())
        self.assertEqual(set((await rename.json())['config']['static_soundbank']['entries']), {'Renamed phrase'})
        delete = await self.client.put('/api/settings', json={'base_revision': 3,
            'config': {'static_soundbank': {'entries': {}}}})
        self.assertEqual(delete.status, 200, await delete.text())
        self.assertEqual((await delete.json())['config']['static_soundbank']['entries'], {})
        self.assertEqual((self.fixture.directory / 'soundbank/hello.wav').read_bytes(), original)
        self.assertFalse((self.fixture.directory / 'soundbank' / entry['file']).exists())
        self.assertEqual((await self.client.get('/api/settings/soundbank/audio/' + entry['file'])).status, 404)

    async def test_signed_draft_preview_survives_serving_node_change_and_rejects_tampering(self):
        value = await self.generate()
        entry = value['entry']
        other = self.fixture.new_cloud()
        with other.locked():
            other.refresh_unlocked(strict=True)
        # A different cache models VIP failover; tokens share only the existing peer key.
        other.soundbank_assets.cache_dir = self.fixture.directory / 'other-cloud-cache/objects'
        reconciliation = SimpleNamespace(store=other, config=self.config)
        authoring = SoundbankAuthoring(reconciliation)
        for file, token in value['previews'].items():
            pcm, mime = authoring.preview(file, token)
            self.assertEqual(mime, 'audio/wav')
            self.assertTrue(pcm.startswith(b'RIFF'))
            with self.assertRaises(SoundbankError):
                authoring.preview(file, token[:-1] + ('0' if token[-1] != '0' else '1'))
        with patch('core.cluster.soundbank_authoring.time.time', return_value=2**40):
            with self.assertRaises(SoundbankError):
                authoring.preview(entry['file'], value['previews'][entry['file']])

    async def test_generation_conflict_upload_failure_and_busy_leave_configuration_unchanged(self):
        r = await self.client.post('/api/settings/soundbank/generate',
            json={'text': 'Test', 'title': 'Test', 'base_revision': 2})
        self.assertEqual(r.status, 409)
        self.pcm.assert_not_awaited()
        self.fixture.drive.fail_upload = True
        r = await self.client.post('/api/settings/soundbank/generate',
            json={'text': 'Test', 'title': 'Test', 'base_revision': 1})
        self.assertEqual((await self.settle(r))[0], 503)
        self.assertEqual(self.store.revision_unlocked(), 1)
        self.assertIsNone(self.store.runtime_snapshot)
        async with self.service.authoring.lock:
            r = await self.client.post('/api/settings/soundbank/generate',
                json={'text': 'Test', 'title': 'Test', 'base_revision': 1})
            self.assertEqual(r.status, 409)

    async def test_optimize_cloud_draft_and_path_guards(self):
        value = await self.generate()
        entry = value['entry']
        r = await self.client.post('/api/settings/soundbank/optimize', json={'file': entry['file'],
            'base_revision': 1, 'preview_token': value['previews'][entry['file']]})
        status, result = await self.settle(r)
        self.assertEqual(status, 200, result)
        self.assertIn('cloud', result['optimized'])
        r = await self.client.post('/api/settings/soundbank/optimize', json={'file': '../private.wav', 'base_revision': 1})
        self.assertEqual((await self.settle(r))[0], 404)
        self.assertEqual((await self.client.post('/api/settings/soundbank/cleanup', json={})).status, 404)

    async def test_worker_discovery_requires_saved_revision_and_consistent_voice(self):
        from core.cluster import tts_protocol as wire
        client = self.service.client
        async def request(subject, payload, timeout):
            node = subject.split('.')[3]
            return SimpleNamespace(data=wire.pack({'protocol': wire.PROTOCOL, 'worker_id': node,
                'revision': 1, 'fingerprint': 'a' * 64, 'state': 'idle'}))
        client.request = request
        expected = SimpleNamespace(pcm=b'\0\0', sample_rate=16000)
        with patch.object(TTSPool, 'generate', AsyncMock(return_value=expected)) as synth:
            self.assertIs(await self.service.authoring._worker_pcm(1, 'Test'), expected)
            synth.assert_awaited_once_with(0, 'Test')
            with self.assertRaises(SoundbankError) as error:
                await self.service.authoring._worker_pcm(2, 'Test')
            self.assertEqual(error.exception.status, 409)

    async def test_accepted_job_does_not_depend_on_initial_http_request_lifetime(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def delayed(revision, text):
            started.set()
            await release.wait()
            return SimpleNamespace(pcm=b'\0\0' * 3200, sample_rate=16000)
        self.service.authoring.generate_pcm = delayed
        r = await self.client.post('/api/settings/soundbank/generate',
            json={'text': 'Test', 'title': 'Test', 'base_revision': 1})
        self.assertEqual(r.status, 202)
        value = await r.json()
        await started.wait()
        status = await (await self.client.get(value['poll_url'])).json()
        self.assertEqual(status['state'], 'pending')
        busy = await self.client.post('/api/settings/soundbank/generate',
            json={'text': 'Test', 'title': 'Test', 'base_revision': 1})
        self.assertEqual(busy.status, 409)
        release.set()
        for _ in range(100):
            status = await (await self.client.get(value['poll_url'])).json()
            if status['state'] != 'pending': break
            await asyncio.sleep(.01)
        self.assertEqual(status['state'], 'complete')
        self.assertEqual(self.store.revision_unlocked(), 1)

    async def test_job_poll_and_draft_preview_work_from_another_control_plane(self):
        from dataclasses import replace
        from config.config_store import LocalConfigStore
        from core.cluster.secret_transport import SecretCipher
        obj = copy.deepcopy(self.store.desired_snapshot['payload']['object'])
        for node in ('test-b', 'test-c'):
            obj['layers']['nodes'][node] = copy.deepcopy(obj['layers']['nodes']['test-node'])
        self.fixture.drive.publish_object(obj, 2)
        await self.service.reconcile()
        clients = [self.client]
        services = [self.service]
        for node in ('test-b', 'test-c'):
            store = self.fixture.new_cloud()
            store.bootstrap['node_id'] = node
            store.cache_dir = self.fixture.directory / node / 'cloud-config'
            store.cache_io = LocalConfigStore(store.cache_dir / '.config.yaml')
            store.soundbank_assets.cache_dir = self.fixture.directory / node / 'cloud-soundbank/objects'
            peers = SecretProvisionConfig(b'k' * 32, tuple((name, 'http://127.0.0.1:8004')
                for name in ('test-node', 'test-b', 'test-c')))
            app = create_app(store, replace(self.config, secrets=peers),
                             client_factory=FakeClient, soundbank_generate=self.pcm)
            client = TestClient(TestServer(app))
            await client.start_server()
            self.addAsyncCleanup(client.close)
            clients.append(client); services.append(app[RECONCILIATION_KEY])
        peers = SecretProvisionConfig(b'k' * 32, tuple((node, str(client.make_url('')).rstrip('/'))
            for node, client in zip(('test-node', 'test-b', 'test-c'), clients)))
        for service in services:
            service.config = replace(service.config, secrets=peers)
            service.authoring.cipher = SecretCipher(peers)
        response = await self.client.post('/api/settings/soundbank/generate',
            json={'text': 'Test draft', 'title': 'Test draft', 'base_revision': 2})
        job = await response.json()
        for _ in range(100):
            reply = await clients[1].get(job['poll_url'])
            self.assertEqual(reply.status, 200, await reply.text())
            status = await reply.json()
            if status['state'] != 'pending': break
            await asyncio.sleep(.01)
        self.assertEqual(status['state'], 'complete', status)
        result = status['result']
        for file, token in result['previews'].items():
            audio = await clients[2].get('/api/settings/soundbank/audio/' + file,
                                        params={'preview_token': token})
            self.assertEqual(audio.status, 200, await audio.text() if audio.status != 200 else '')
        # Saving from another serving node does not require process-local runtime files.
        save = await clients[1].put('/api/settings', json={'base_revision': 2,
            'config': {'static_soundbank': {'entries': {'Test draft': result['entry']}}}})
        self.assertEqual(save.status, 200, await save.text())
        self.assertEqual(self.fixture.drive.manifest['revision'], 3)
        self.assertIsNone(services[1].store.runtime_snapshot)

    async def test_real_segment_contract_uses_one_free_worker_and_releases_it(self):
        from core.cluster.tts_worker import TTSService
        from core.cluster.activity import Activity
        from test_worker_llm_stream import Bus
        from test_worker_tts_segments import Engine, bundle
        bus, workers, engines = Bus(), [], []
        previous = self.service.client
        self.service.client = bus
        try:
            for node in self.service.authoring.nodes:
                engine, activity = Engine(), Activity(None, node)
                engine.release.set()
                activity.counts['tts'] = 0
                configuration = bundle(node)
                configuration['revision'] = 1
                worker = TTSService(bus, node, configuration, engine, activity)
                await worker.start()
                workers.append(worker); engines.append(engine)
            workers[0].activity.counts['asr'] = 1
            from core.cluster import tts_protocol as wire
            rpc = SimpleNamespace(client=bus, core_id='test-node', stopping=False, calls=set(), streams=set())
            check = TTSPool(rpc, {'revision': 1, 'fingerprint': workers[1].bundle['fingerprint'], 'workers': self.service.authoring.nodes})
            self.assertTrue(await check.idle('test-b'))
            with patch.object(wire, 'MAX_SECONDS', 1):
                result = await self.service.authoring._worker_pcm(1, 'Synthetic audio')
            self.assertNotEqual(result.worker_id, workers[0].worker_id)
            self.assertEqual(result.pcm, bytes(3840))
            self.assertEqual(sum(engine.entered.is_set() for engine in engines), 1)
            self.assertFalse(self.service.authoring.streams)
            self.assertTrue(all(worker.job is None for worker in workers))
        finally:
            self.service.client = previous
            for worker in workers: await worker.stop()

    async def test_private_job_route_rejects_browser_requests_and_bad_public_inputs(self):
        r = await self.client.post('/internal/settings/soundbank/job', json={'job_id': 'a' * 32})
        self.assertEqual(r.status, 403)
        r = await self.client.post('/api/settings/soundbank/generate', json={'text': 'Test', 'base_revision': True})
        self.assertEqual(r.status, 400)
        r = await self.client.get('/api/settings/soundbank/jobs/not-a-job')
        self.assertEqual(r.status, 400)

    async def test_shutdown_cancels_owned_generation_before_cleanup(self):
        started = asyncio.Event()
        async def blocked(revision, text):
            started.set()
            await asyncio.Event().wait()
        self.service.authoring.generate_pcm = blocked
        task = asyncio.create_task(self.service.authoring.generate('Test', revision=1))
        await started.wait()
        await self.service.authoring.stop()
        self.assertTrue(task.cancelled())
        self.assertFalse(self.service.authoring.tasks)
        self.assertEqual(self.store.revision_unlocked(), 1)
