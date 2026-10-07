"""Bounded diagnostics and loopback aggregation; no provider, Drive or NATS I/O."""
import copy
import json
import unittest
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core.api.control_plane_settings import ControlPlaneSettingsHandler
from core.cluster.voice_diagnostics import PROTOCOL, STATES, VoiceDiagnostics, safe_snapshot


def snapshot():
    recorder = VoiceDiagnostics()
    recorder.record('asr_admitted', 'a' * 32, worker_id='deskb2x', generation=1, elapsed_ms=20)
    return {'protocol': PROTOCOL, 'node_id': 'deskb1x', 'sessions': dict.fromkeys(STATES, 0),
            'events': recorder.snapshot(), 'mcp': False}


class MetadataTests(unittest.TestCase):
    def test_bounded_allowlist_and_peer_schema(self):
        recorder = VoiceDiagnostics()
        for _ in range(100):
            recorder.record('turn_failed', 'a' * 32, code='asr_unavailable', password='private',
                            transcript='private', worker_id='credential://private')
        self.assertEqual(len(recorder.snapshot()), 64)
        self.assertNotIn('private', json.dumps(recorder.snapshot()))
        value = snapshot()
        self.assertEqual(safe_snapshot(value, 'deskb1x'), value)
        for field, replacement in (('at', 'private-reference-name'), ('session_id', None), ('event', [])):
            unsafe = copy.deepcopy(value)
            unsafe['events'][0][field] = replacement
            with self.subTest(field=field), self.assertRaises(ValueError):
                safe_snapshot(unsafe, 'deskb1x')
        value['events'][0]['password'] = 'private'
        with self.assertRaises(ValueError):
            safe_snapshot(value, 'deskb1x')


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.payload = snapshot()
        async def diagnostics(request):
            return web.json_response(self.payload)
        app = web.Application()
        app.router.add_get('/diagnostics', diagnostics)
        self.peer = TestServer(app)
        await self.peer.start_server()
        self.addAsyncCleanup(self.peer.close)
        config = SimpleNamespace(allow_remote=False, diagnostic_core_port=self.peer.port,
            secrets=SimpleNamespace(nodes=(('deskb1x', 'http://127.0.0.1:8004'),)))
        service = SimpleNamespace(config=config, store=object(), source={'node_id': 'deskb1x'})
        handler = ControlPlaneSettingsHandler(service)
        app = web.Application()
        app.router.add_get('/api/cluster/diagnostics', handler.handle_voice_diagnostics)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_configured_peer_and_query_rejection(self):
        response = await self.client.get('/api/cluster/diagnostics')
        self.assertEqual(response.status, 200)
        self.assertIn('no-store', response.headers['Cache-Control'])
        result = await response.json()
        self.assertEqual(result['scope'], 'configured_peers')
        self.assertEqual(result['nodes'], [{'state': 'ready', **self.payload}])
        self.assertEqual((await self.client.get('/api/cluster/diagnostics?url=http://example.com')).status, 400)

    async def test_unsafe_or_unavailable_peer_never_exports_raw_data(self):
        self.payload['password'] = 'private'
        response = await self.client.get('/api/cluster/diagnostics')
        self.assertEqual((await response.json())['nodes'], [{'node_id': 'deskb1x', 'state': 'unavailable'}])
        await self.peer.close()
        response = await self.client.get('/api/cluster/diagnostics')
        self.assertEqual((await response.json())['nodes'], [{'node_id': 'deskb1x', 'state': 'unavailable'}])


if __name__ == '__main__':
    unittest.main()
