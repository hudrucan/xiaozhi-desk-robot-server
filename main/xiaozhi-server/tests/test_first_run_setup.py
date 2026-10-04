"""First-run isolation and browser recovery with fake OAuth/Drive only."""

import asyncio
import errno
import importlib.util
import io
import json
import logging
from pathlib import Path
import queue
import threading
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import urlencode

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer
from google.oauth2.credentials import Credentials
import yaml

from config.cloud_recovery import RecoveryConflict, RecoveryError
from config.cloud_restore import restore_cloud_node
from config.config_store import canonical_bytes
from config.first_run import FirstRunError, finish_first_run, prepare_first_run
from config.recovery_oauth import OAuthBusyError, OAuthLoginError, OAuthLoopbackSession, OAuthPortError
from core.api.setup_handler import SetupHandler
from core.setup_server import create_setup_app
import test_cloud_recovery as recovery_tests

CLIENT, CREDENTIALS, PRIVATE = recovery_tests.CLIENT, recovery_tests.CREDENTIALS, recovery_tests.PRIVATE


class FirstRunTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data = Path(self.temporary.name) / "data"

    def test_empty_install_has_private_seed_and_durable_marker(self):
        self.assertTrue(prepare_first_run(self.data))
        self.assertEqual(yaml.safe_load((self.data / ".config.yaml").read_bytes()), {})
        self.assertEqual(self.data.stat().st_mode & 0o777, 0o700)
        for name in (".config.yaml", ".first-run"):
            self.assertEqual((self.data / name).stat().st_mode & 0o777, 0o600)
        self.assertTrue(prepare_first_run(self.data))

    def test_existing_and_partial_installs_are_never_seeded_or_repaired(self):
        for name, content in ((".config.yaml", b"broken: ["), ("bootstrap.yaml", b"config_provider: google_drive\n"),
                              ("config.d", None), ("node-secrets", None), (".cloud-recovery", None)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                data = Path(folder)
                if content is None:
                    (data / name).mkdir()
                else:
                    (data / name).write_bytes(content)
                self.assertFalse(prepare_first_run(data))
                self.assertFalse((data / ".first-run").exists())
                if content is not None:
                    self.assertEqual((data / name).read_bytes(), content)

    def test_symlink_state_rejects_and_interrupted_seed_stays_setup(self):
        self.data.symlink_to(self.temporary.name)
        with self.assertRaises(FirstRunError):
            prepare_first_run(self.data)
        self.data.unlink()
        self.data.mkdir()
        (self.data / "bootstrap.yaml").symlink_to(self.data / "missing")
        self.assertFalse(prepare_first_run(self.data))
        (self.data / "bootstrap.yaml").unlink()
        with patch("config.first_run._create", side_effect=[None, OSError(PRIVATE)]):
            with self.assertRaises(FirstRunError):
                prepare_first_run(self.data)
        (self.data / ".first-run").write_bytes(b"first-run-v1\n")
        self.assertTrue(prepare_first_run(self.data))

    def test_marker_removal_is_fsynced_and_failed_fsync_keeps_setup(self):
        prepare_first_run(self.data)
        with patch("config.first_run.fsync_directory", side_effect=[OSError(PRIVATE), None]):
            with self.assertRaises(FirstRunError):
                finish_first_run(self.data)
        self.assertTrue((self.data / ".first-run").exists())
        finish_first_run(self.data)
        self.assertFalse(prepare_first_run(self.data))


class OAuthSessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.client = Path(self.temporary.name) / "client.json"
        self.client.write_text(json.dumps(CLIENT))
        self.callbacks = queue.Queue()
        creds = Credentials(token="private-access", refresh_token="test-refresh-private", token_uri="https://oauth2.googleapis.com/token",
                            client_id="test-client", client_secret="client-private")
        self.flow = Mock(credentials=creds)
        self.flow.authorization_url.side_effect = lambda **kw: ("https://accounts.google.com/test", kw["state"])
        self.factory = Mock(return_value=self.flow)
        self.server = Mock()

        def handle():
            try:
                self.session._accept_callback(self.callbacks.get(timeout=0.01))
            except queue.Empty:
                pass

        self.server.handle_request.side_effect = handle
        self.server_factory = Mock(return_value=self.server)
        self.session = OAuthLoopbackSession(self.client, flow_factory=self.factory, server_factory=self.server_factory)
        self.addCleanup(self.session.close)

    def test_pkce_fixed_callback_correct_state_and_no_early_persistence(self):
        self.assertEqual(self.session.start(), "https://accounts.google.com/test")
        self.assertEqual(self.server_factory.call_args.args[0], ("127.0.0.1", 8765))
        self.assertTrue(self.factory.call_args.kwargs["autogenerate_code_verifier"])
        self.assertEqual(self.factory.call_args.kwargs["scopes"], ["https://www.googleapis.com/auth/drive.file"])
        self.assertEqual(self.flow.redirect_uri, "http://127.0.0.1:8765/")
        self.callbacks.put("/?" + urlencode({"state": self.session._state, "code": PRIVATE}))
        content = self.session.wait()
        self.assertEqual(json.loads(content)["type"], "authorized_user")
        self.assertEqual(self.session.status(), {"status": "authorized"})
        self.assertEqual(set(p.name for p in self.client.parent.iterdir()), {"client.json"})
        self.assertIsNone(self.session._state)
        self.assertIsNone(self.session._flow)

    def test_one_session_and_state_mismatch_rejected(self):
        self.session.start()
        other = OAuthLoopbackSession(self.client, flow_factory=self.factory, server_factory=self.server_factory)
        with self.assertRaises(OAuthBusyError):
            other.start()
        self.callbacks.put("/?" + urlencode({"state": "wrong", "code": PRIVATE}))
        with self.assertRaises(OAuthLoginError) as raised:
            self.session.wait()
        self.assertNotIn(PRIVATE, str(raised.exception))
        self.flow.fetch_token.assert_not_called()
        self.assertIsNone(OAuthLoopbackSession._owner)

    def test_timeout_cleanup_and_port_collision_are_sanitized(self):
        self.session.start()
        self.session._deadline = time.monotonic() - 1
        with self.assertRaises(OAuthLoginError):
            self.session.wait()
        other = OAuthLoopbackSession(self.client, flow_factory=self.factory,
            server_factory=Mock(side_effect=OSError(errno.EADDRINUSE, PRIVATE)))
        with self.assertRaises(OAuthPortError) as raised:
            other.start()
        self.assertIn("port", str(raised.exception))
        self.assertNotIn(PRIVATE, str(raised.exception))
        self.assertIsNone(OAuthLoopbackSession._owner)

    def test_token_logging_and_callback_logging_are_suppressed(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        initial_level = logging.root.manager.disable
        self.flow.fetch_token.side_effect = lambda **kw: logging.getLogger("oauthlib").critical(PRIVATE)
        self.session.start()
        callback_handler = self.server_factory.call_args.args[1]
        callback_handler.log_message(None, PRIVATE)
        self.callbacks.put("/?" + urlencode({"state": self.session._state, "code": PRIVATE}))
        self.session.wait()
        self.session.close()
        self.assertNotIn(PRIVATE, output.getvalue())
        self.assertEqual(logging.root.manager.disable, initial_level)
        self.assertNotIn("refresh", str(self.session.status()))


class SetupAPIFixture:
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data = Path(self.temporary.name) / "data"
        prepare_first_run(self.data)
        self.handler = SetupHandler(Mock(), data_dir=self.data)
        self.client = TestClient(TestServer(create_setup_app(Mock(), handler=self.handler)))
        self.addAsyncCleanup(self.client.close)
        await self.client.start_server()

    async def post(self, path, **kwargs):
        return await self.client.post("/api/setup/" + path, headers={"X-Setup-Request": "1"}, **kwargs)

    async def upload(self, value, filename="client.json"):
        form = FormData()
        form.add_field("file", value if isinstance(value, bytes) else json.dumps(value).encode(),
                       filename=filename, content_type="application/json")
        return await self.post("oauth-client", data=form)

    def authorize_files(self):
        (self.data / "oauth-client.json").write_text(json.dumps(CLIENT))
        (self.data / "drive-credentials.json").write_bytes(CREDENTIALS)


class SetupAPITests(SetupAPIFixture, unittest.IsolatedAsyncioTestCase):
    async def test_desktop_upload_private_and_metadata_only(self):
        response = await self.upload(CLIENT)
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"client_installed": True})
        self.assertEqual((self.data / "oauth-client.json").stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.data / "drive-credentials.json").exists())

    async def test_invalid_uploads_and_different_identity_never_overwrite(self):
        for value in ({"web": CLIENT["installed"]}, b"broken JSON", {"installed": {**CLIENT["installed"], "token_uri": PRIVATE}}):
            response = await self.upload(value)
            self.assertGreaterEqual(response.status, 400)
            self.assertNotIn(PRIVATE, await response.text())
            self.assertFalse((self.data / "oauth-client.json").exists())
        self.assertEqual((await self.upload(b"x" * (33 * 1024))).status, 413)
        self.assertEqual((await self.upload(CLIENT, "../../client.json")).status, 400)
        self.assertEqual((await self.upload(CLIENT)).status, 200)
        original = (self.data / "oauth-client.json").read_bytes()
        response = await self.upload({"installed": {**CLIENT["installed"], "client_id": "other"}})
        self.assertGreaterEqual(response.status, 400)
        self.assertEqual((self.data / "oauth-client.json").read_bytes(), original)

    async def test_symlink_upload_does_not_touch_target(self):
        target = Path(self.temporary.name) / "target"
        target.write_bytes(b"original")
        (self.data / "oauth-client.json").symlink_to(target)
        self.assertGreaterEqual((await self.upload(CLIENT)).status, 400)
        self.assertEqual(target.read_bytes(), b"original")

    async def test_origin_host_and_mutation_header_required(self):
        response = await self.client.post("/api/setup/oauth/start")
        self.assertEqual(response.status, 403)
        response = await self.client.get("/api/setup/status", headers={"Origin": "https://other.example"})
        self.assertEqual(response.status, 403)
        response = await self.client.get("/setup/", headers={"Host": "other.example"})
        self.assertEqual(response.status, 403)
        response = await self.client.get("/setup/")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual((await self.client.get("/xiaozhi/ota/")).status, 404)
        self.assertEqual((await self.client.get("/settings/")).status, 404)

    async def test_async_oauth_owns_wait_and_persists_without_exposing_credentials(self):
        await self.upload(CLIENT)
        gate = threading.Event()

        def wait():
            gate.wait(2)
            return CREDENTIALS

        session = Mock(start=Mock(return_value="https://accounts.google.com/test"), wait=Mock(side_effect=wait))
        self.handler.session_factory = Mock(return_value=session)
        response = await self.post("oauth/start")
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["authorization_url"], "https://accounts.google.com/test")
        self.assertEqual((await self.post("oauth/start")).status, 409)
        self.assertEqual(await (await self.client.get("/api/setup/oauth/status")).json(), {"status": "pending"})
        gate.set()
        await self.handler._oauth_task
        self.assertEqual(await (await self.client.get("/api/setup/oauth/status")).json(), {"status": "authorized"})
        self.assertEqual((self.data / "drive-credentials.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.handler.session_factory.call_args.kwargs, {"port": 8765, "timeout": 600})
        self.assertFalse((self.data / "bootstrap.yaml").exists())

    async def test_cancelled_oauth_start_closes_the_owned_session(self):
        gate = threading.Event()
        entered = threading.Event()

        def start():
            entered.set()
            gate.wait(2)
            return "https://accounts.google.com/test"

        session = Mock(start=Mock(side_effect=start))
        self.handler.session_factory = Mock(return_value=session)
        task = asyncio.create_task(self.handler.handle_oauth_start(Mock()))
        await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        gate.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        session.close.assert_called_once()
        self.assertIsNone(self.handler._oauth)

    async def test_safe_discovery_and_hostname_preselection(self):
        self.authorize_files()
        source = Mock(metadata=Mock(return_value={"source_id": "source", "label": "Desk", "nodes": ["deskbox", "other"]}))
        self.handler.discover = Mock(return_value=[source])
        self.handler.transport_factory = Mock()
        with patch("core.api.setup_handler.hostname_node_id", return_value="deskbox"):
            response = await self.client.get("/api/setup/sources")
        body = await response.json()
        self.assertEqual(body["selected_node_id"], "deskbox")
        self.assertEqual(set(body["sources"][0]), {"source_id", "label", "nodes"})
        for private in ("file_id", "secrets", "refresh_token", "manifest", "client_secret"):
            self.assertNotIn(private, json.dumps(body))
        source.metadata.return_value["nodes"] = ["only"]
        response = await self.client.get("/api/setup/sources")
        self.assertEqual((await response.json())["selected_node_id"], "only")


class SetupRestoreTests(SetupAPIFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.fixture = recovery_tests.CloudRecoveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.provision()
        self.authorize_files()
        self.handler.transport_factory = Mock(return_value=self.fixture.drive)

        def restore(*args, **kwargs):
            return restore_cloud_node(*args, **kwargs, default_path=self.fixture.default_path)

        self.handler.restorer = Mock(side_effect=restore)
        self.body = {"source_id": self.fixture.source().descriptor["source_id"], "node_id": "test-node", "passphrase": recovery_tests.PASSPHRASE}

    async def test_failed_passphrase_and_preflight_do_not_publish_or_remove_marker(self):
        response = await self.post("restore", json={**self.body, "passphrase": "wrong"})
        self.assertEqual(response.status, 503)
        self.assertTrue((self.data / ".first-run").exists())
        self.assertFalse((self.data / "bootstrap.yaml").exists())
        with patch("config.cloud_restore.GoogleDriveConfigStore.verify_recovery_readiness", side_effect=RecoveryError()):
            response = await self.post("restore", json=self.body)
        self.assertEqual(response.status, 503)
        self.assertFalse((self.data / "bootstrap.yaml").exists())
        self.assertTrue((self.data / ".first-run").exists())

    async def test_conflict_keeps_marker_and_returns_409(self):
        self.handler.restorer.side_effect = RecoveryConflict()
        response = await self.post("restore", json=self.body)
        self.assertEqual(response.status, 409)
        self.assertTrue((self.data / ".first-run").exists())
        self.assertFalse((self.data / "bootstrap.yaml").exists())

    async def test_success_rediscovery_bootstrap_last_then_marker_removed(self):
        discover = self.handler.discover
        self.handler.discover = Mock(side_effect=discover)
        await self.client.get("/api/setup/sources")
        events = []
        finish = finish_first_run

        def complete(data):
            self.assertEqual(yaml.safe_load((data / "bootstrap.yaml").read_bytes())["config_provider"], "google_drive")
            self.assertTrue((data / ".first-run").exists())
            events.append("finish")
            finish(data)

        with patch("core.api.setup_handler.finish_first_run", side_effect=complete):
            response = await self.post("restore", json=self.body)
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"config": "OK", "soundbank": "OK", "memory": "OK", "secrets": "OK", "provider": "google_drive"})
        self.assertEqual(self.handler.discover.call_count, 2)
        self.assertTrue(self.handler.restorer.call_args.kwargs["activate"])
        self.assertEqual(events, ["finish"])
        self.assertFalse((self.data / ".first-run").exists())
        self.assertEqual((await self.post("oauth/start")).status, 409)
        self.assertEqual((await self.post("restart")).status, 200)

    async def test_restore_disconnect_finishes_transaction_and_allows_restart(self):
        gate, entered = threading.Event(), threading.Event()
        restorer = self.handler.restorer

        def restore(*args, **kwargs):
            entered.set()
            gate.wait(2)
            return restorer(*args, **kwargs)

        self.handler.restorer = Mock(side_effect=restore)
        request = Mock(content_type="application/json", json=AsyncMock(return_value=dict(self.body)))
        task = asyncio.create_task(self.handler.handle_restore(request))
        await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        gate.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.handler._completed)
        self.assertFalse((self.data / ".first-run").exists())
        self.assertEqual((await self.post("restart")).status, 200)


class StartupIsolationTests(unittest.IsolatedAsyncioTestCase):
    def application(self):
        path = Path(__file__).resolve().parents[1] / "app.py"
        spec = importlib.util.spec_from_file_location("setup_application_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    async def test_setup_dispatch_does_not_import_or_start_runtime(self):
        app = self.application()
        forbidden = {"core.websocket_server", "core.http_server", "core.utils.util", "core.utils.modules_initialize"}
        original_import = __import__

        def checked(name, *args, **kwargs):
            self.assertNotIn(name, forbidden)
            return original_import(name, *args, **kwargs)

        with patch("config.first_run.prepare_first_run", return_value=True), \
                patch("core.setup_server.run_setup", new=AsyncMock()) as setup, \
                patch.object(app, "run_normal", new=AsyncMock()) as normal, patch("builtins.__import__", side_effect=checked):
            await app.main()
        setup.assert_awaited_once()
        normal.assert_not_awaited()

    async def test_normal_dispatch_and_routes_have_no_setup_handler(self):
        app = self.application()
        with patch("config.first_run.prepare_first_run", return_value=False), patch.object(app, "run_normal", new=AsyncMock()) as normal, \
                patch("core.setup_server.run_setup", new=AsyncMock()) as setup:
            await app.main()
        normal.assert_awaited_once()
        setup.assert_not_awaited()
        normal_http = (Path(__file__).resolve().parents[1] / "core/http_server.py").read_text()
        self.assertNotIn("/api/setup/", normal_http)
        self.assertNotIn("SetupHandler", normal_http)

    async def test_setup_handler_construction_defers_audio_recovery_imports(self):
        path = Path(__file__).resolve().parents[1] / "core/api/setup_handler.py"
        spec = importlib.util.spec_from_file_location("isolated_setup_handler", path)
        module = importlib.util.module_from_spec(spec)
        original_import = __import__

        def checked(name, *args, **kwargs):
            self.assertNotIn(name, {"config.cloud_restore", "core.soundbank", "core.websocket_server", "core.http_server"})
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=checked):
            spec.loader.exec_module(module)
            handler = module.SetupHandler(Mock())
        self.assertIsNone(handler.restorer)
