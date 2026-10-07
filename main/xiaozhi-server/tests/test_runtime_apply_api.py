"""Settings maintenance API access boundaries with fake Cloud/NATS and installer."""
import json
import os
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

from config.cloud_layers import initial_cluster_object
from config.control_plane import ControlPlaneConfig
from config.cloud_secrets import LocalSecretStore
from config.google_drive_config import GoogleDriveConfigStore
from core.cluster.runtime_apply import RuntimeApply
from core.cluster.secret_transport import SecretProvisionConfig, SecretCipher, canonical
from core.control_plane import create_app
import test_cloud_config as fixtures
from test_control_plane import ENV, FakeClient

PEERS = SecretProvisionConfig(bytes(range(32)), (
    ('node-a', 'http://127.0.0.1:8004'), ('node-b', 'http://127.0.0.2:8004'),
    ('node-c', 'http://127.0.0.3:8004')))
OP = 'a' * 32


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.CloudConfigTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        obj = initial_cluster_object(self.fixture.cloud_overrides, 'node-a')
        self.fixture.drive.publish_object(obj, 1)
        secrets = LocalSecretStore('node-a', self.fixture.directory / 'runtime-secrets')
        secrets.put_many(self.fixture.secrets.export_dataset()['values'])
        store = GoogleDriveConfigStore({**self.fixture.bootstrap, 'node_id': 'node-a'},
            transport=self.fixture.drive, cache_dir=self.fixture.directory / 'runtime-cache',
            default_path=str(self.fixture.default_path), secret_provider=secrets)
        with patch.dict(os.environ, ENV, clear=True): config = ControlPlaneConfig.from_env()
        config = replace(config, host='127.0.0.1', allow_remote=True, secrets=PEERS, runtime_apply=True)
        self.runtime = AsyncMock()
        self.runtime.node = 'node-a'
        self.runtime.cipher = SecretCipher(PEERS)
        self.runtime.status.return_value = {'protocol': 'xiaozhi-runtime-apply-v1', 'ready_nodes': 0, 'nodes': []}
        self.runtime.start.return_value = {'state': 'running', 'revision': 1}
        self.runtime.receive.return_value = {'node_id': 'node-a', 'state': 'prepared'}
        with patch('core.cluster.runtime_apply.RuntimeApply', return_value=self.runtime):
            app = create_app(store, config, client_factory=FakeClient)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_real_action_capability_and_asset_exist_without_legacy_restart(self):
        value = await (await self.client.get('/api/settings/capabilities')).json()
        self.assertTrue(value['capabilities']['runtime_apply'])
        self.assertFalse(value['capabilities']['restart'])
        self.assertEqual((await self.client.get('/settings/runtime_apply.js')).status, 200)
        self.assertEqual((await self.client.post('/api/settings/restart')).status, 404)
        response = await self.client.get('/api/settings/runtime')
        self.assertEqual(response.status, 200)
        self.assertIn('no-store', response.headers['Cache-Control'])

    async def test_apply_requires_explicit_same_origin_saved_revision_request(self):
        payload = {'revision': 1, 'recover': False}
        response = await self.client.post('/api/settings/runtime', json=payload)
        self.assertEqual(response.status, 403)
        for headers in ({'X-Xiaozhi-Settings': '1', 'Origin': 'http://attacker.invalid'},
                        {'X-Xiaozhi-Settings': '1', 'Sec-Fetch-Site': 'cross-site'}):
            self.assertEqual((await self.client.post('/api/settings/runtime', json=payload, headers=headers)).status, 403)
        self.runtime.start.assert_not_called()
        response = await self.client.post('/api/settings/runtime', json=payload,
                                           headers={'X-Xiaozhi-Settings': '1'})
        self.assertEqual(response.status, 202)
        self.runtime.start.assert_awaited_once_with(1, False)

    async def test_paths_services_unsaved_config_and_oversize_body_are_rejected(self):
        headers = {'X-Xiaozhi-Settings': '1'}
        for body in ({'revision': 1, 'recover': False, 'service': 'ssh'},
                     {'revision': 1, 'recover': False, 'config': {'prompt': 'unsaved'}},
                     {'revision': 1, 'recover': False, 'path': '/etc/passwd'}):
            self.assertEqual((await self.client.post('/api/settings/runtime', json=body, headers=headers)).status, 400)
        response = await self.client.post('/api/settings/runtime', data=b' ' * 8193,
                                          headers={**headers, 'Content-Type': 'application/json'})
        self.assertEqual(response.status, 400)
        self.runtime.start.assert_not_called()

    async def test_private_commands_require_cipher_identity_source_and_action(self):
        body = {'command': {'action': 'prepare', 'operation': OP, 'revision': 1, 'request_id': 'b'*32}}
        for source, action in (('node-b', 'runtime-request'), ('node-a', 'store')):
            envelope = self.runtime.cipher.seal(action, source, 'node-a', OP, body)
            response = await self.client.post('/internal/settings/runtime', json=envelope)
            self.assertEqual(response.status, 403)
        self.runtime.receive.assert_not_called()
        envelope = self.runtime.cipher.seal('runtime-request', 'node-a', 'node-a', OP, body)
        response = await self.client.post('/internal/settings/runtime', json=envelope)
        self.assertEqual(response.status, 200)
        _, _, result = self.runtime.cipher.open(canonical(await response.json()),
            'runtime-response', 'node-a', source='node-a', operation=OP)
        self.assertEqual(result, self.runtime.receive.return_value)

    async def test_raw_installer_error_is_not_returned_to_browser(self):
        self.runtime.start.side_effect = OSError('private-fixture-key private-credential-path')
        response = await self.client.post('/api/settings/runtime', json={'revision': 1, 'recover': False},
                                          headers={'X-Xiaozhi-Settings': '1'})
        self.assertEqual(response.status, 503)
        self.assertNotIn('private-fixture', await response.text())


class StartupTests(unittest.TestCase):
    def test_opt_in_requires_authenticated_private_peer_configuration(self):
        with patch.dict(os.environ, {**ENV, 'XIAOZHI_RUNTIME_APPLY_ENABLED': 'true'}, clear=True):
            with self.assertRaises(ValueError): ControlPlaneConfig.from_env()
        with patch.dict(os.environ, {**ENV, 'XIAOZHI_RUNTIME_APPLY_ENABLED': 'false'}, clear=True):
            self.assertFalse(ControlPlaneConfig.from_env().runtime_apply)


if __name__ == '__main__':
    unittest.main()
