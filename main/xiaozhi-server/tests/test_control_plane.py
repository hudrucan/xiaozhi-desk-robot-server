"""Standalone control plane: disposable data, fake Cloud/NATS and loopback HTTP only."""

import asyncio
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest.mock import Mock, patch

import yaml
from aiohttp.test_utils import TestClient, TestServer

from config.cloud_layers import initial_cluster_object
from config.control_plane import ControlPlaneConfig
from core.api.control_plane_settings import ControlPlaneSettingsHandler
from core.cluster.config_protocol import CONFIG_CHANGED_SUBJECT, MAX_EVENT_BYTES, config_changed, parse_config_changed
from core.cluster.config_reconciliation import ConfigReconciliation
from core.cluster.nats_config import NatsConfig, NatsConnectionConfig, WorkerConfigError
from core.control_plane import RECONCILIATION_KEY, create_app
import test_cloud_config as fixtures

ENV = {"XIAOZHI_NATS_SERVERS": "nats://10.10.10.11:4222,nats://10.10.10.12:4222,nats://10.10.10.13:4222",
       "XIAOZHI_NATS_USER": "xiaozhi", "XIAOZHI_NATS_PASSWORD": "fake-nats-private-password"}


class FakeClient:
    def __init__(self):
        self.is_connected, self.is_closed = False, False
        self.options = {}
        self.subscriptions, self.messages = [], []
        self.fail_publish = False
        self.fail_connect = False
        self.drained = False

    async def connect(self, **options):
        self.options = options
        if self.fail_connect:
            raise OSError("fake-nats-private-password credential-bearing-url")
        self.is_connected = True

    async def subscribe(self, subject, **options):
        self.subscriptions.append((subject, options))

    async def publish(self, subject, data):
        if self.fail_publish:
            raise OSError("fake-nats-private-password credential-bearing-url")
        self.messages.append((subject, data))

    async def flush(self, **options):
        pass

    async def close(self):
        self.is_closed, self.is_connected = True, False
        if self.options.get("closed_cb"):
            await self.options["closed_cb"]()

    async def drain(self):
        self.drained = True
        await self.close()


def make_fixture():
    fixture = fixtures.CloudConfigTests()
    fixture.setUp()
    fixture.defaults["cluster"] = {"ingress": {"vip": "192.168.1.186"}}
    fixture.default_path.write_text(yaml.safe_dump(fixture.defaults))
    obj = initial_cluster_object(copy.deepcopy(fixture.cloud_overrides), "test-node")
    fixture.drive.publish_object(obj, 1)
    store = fixture.new_cloud()
    with patch.dict(os.environ, ENV, clear=True):
        config = ControlPlaneConfig.from_env()
    return fixture, store, config


async def until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(wait(), timeout=3)


class ReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture, self.store, self.config = make_fixture()
        self.addCleanup(self.fixture.doCleanups)
        self.client = FakeClient()
        self.service = ConfigReconciliation(self.store, self.config, client_factory=lambda: self.client)
        await self.service.start()
        self.addAsyncCleanup(self.service.stop)
        await until(lambda: self.service.nats_state == "connected")

    def publish_revision(self, revision):
        obj = json.loads(self.fixture.drive.files[self.fixture.drive.manifest["config"]["file_id"]])
        obj["layers"]["cluster"]["prompt"] = f"Desired revision {revision}"
        self.fixture.drive.publish_object(obj, revision)

    async def hint(self, data):
        await self.service._on_hint(types.SimpleNamespace(data=data))

    async def test_broadcast_subscription_and_indefinite_reconnect_options(self):
        subject, options = self.client.subscriptions[0]
        self.assertEqual(subject, CONFIG_CHANGED_SUBJECT)
        self.assertNotIn("queue", options)
        self.assertEqual(self.client.options["max_reconnect_attempts"], -1)
        self.assertTrue(self.client.options["allow_reconnect"])
        self.assertEqual(self.client.options["servers"], list(self.config.nats.servers))
        self.assertEqual(options["pending_bytes_limit"], 64 * MAX_EVENT_BYTES)

    async def test_newer_hint_refreshes_live_cloud_without_trusting_hint_revision(self):
        self.publish_revision(2)
        with patch.object(self.fixture.drive, "read_manifest", wraps=self.fixture.drive.read_manifest) as read:
            await self.hint(config_changed(999))
            await until(lambda: self.service.source["desired_revision"] == 2)
            await self.hint(config_changed(999))
            await self.hint(config_changed(998))
            await asyncio.sleep(0.01)
            self.assertEqual(read.call_count, 1)
        self.assertIsNone(self.store.active_revision)
        self.assertIsNone(self.store.runtime_snapshot)
        self.assertFalse((self.store.cache_dir / "active.json").exists())

    async def test_older_duplicates_malformed_and_oversize_events_do_no_cloud_io(self):
        with patch.object(self.fixture.drive, "read_manifest", side_effect=AssertionError("Unexpected refresh")):
            for value in (config_changed(1), b"", b"not json", b"\xff", b"x" * (MAX_EVENT_BYTES + 1),
                          b'{"protocol":"xiaozhi-config-v1","revision":true}',
                          b'{"protocol":"xiaozhi-config-v1","revision":2,"password":"private"}'):
                await self.hint(value)
            await asyncio.sleep(0.01)
        self.assertEqual(self.service.source["desired_revision"], 1)

    async def test_missed_hint_repaired_by_periodic_refresh(self):
        await self.service.stop()
        config = ControlPlaneConfig(self.config.nats, reconcile_interval=0.02)
        self.client = FakeClient()
        self.service = ConfigReconciliation(self.store, config, client_factory=lambda: self.client)
        await self.service.start()
        self.addAsyncCleanup(self.service.stop)
        self.publish_revision(2)
        await until(lambda: self.service.source["desired_revision"] == 2 and self.service.reconciliation["state"] == "ready")
        self.assertEqual(self.service.reconciliation["state"], "ready")

    async def test_disconnect_does_not_fail_cached_health_and_reconnect_heals_missed_event(self):
        await self.client.options["disconnected_cb"]()
        self.client.is_connected = False
        self.assertEqual(self.service.nats_state, "disconnected")
        self.assertTrue(self.service.healthy)
        self.publish_revision(2)
        self.client.is_connected = True
        await self.client.options["reconnected_cb"]()
        await until(lambda: self.service.source["desired_revision"] == 2)
        self.assertEqual(len(self.client.subscriptions), 1)  # Client replays the original subscription.

    async def test_initial_failure_and_unexpected_closed_connection_retry(self):
        await self.service.stop()
        failing, succeeding = FakeClient(), FakeClient()
        failing.fail_connect = True
        factory = Mock(side_effect=[failing, succeeding, FakeClient()])
        self.service = ConfigReconciliation(self.store, self.config, client_factory=factory)
        await self.service.start()
        self.addAsyncCleanup(self.service.stop)
        await until(lambda: self.service.nats_state == "connected")
        self.assertEqual(factory.call_count, 2)
        await succeeding.close()
        await until(lambda: factory.call_count == 3)
        await until(lambda: self.service.nats_state == "connected")

    async def test_shutdown_drains_and_unhooks_store_without_active_publication(self):
        await self.service.stop()
        self.assertTrue(self.client.drained)
        self.assertIsNone(self.store.publication_observer)
        self.assertFalse(self.service._tasks)
        self.assertIsNone(self.store.active_revision)

    async def test_failed_refresh_keeps_snapshot_and_redacted_error_timestamps(self):
        self.fixture.drive.offline = True
        await self.service.reconcile()
        payload = self.service.status()
        self.assertTrue(self.service.healthy)
        self.assertEqual(payload["reconciliation"]["error_code"], "cloud_refresh_failed")
        self.assertIsNotNone(payload["reconciliation"]["last_error_at"])
        self.assertIsNotNone(payload["reconciliation"]["last_success_at"])

    async def test_refresh_observes_existing_app_active_file_without_marking_applied(self):
        from config.config_store import checksum, canonical_bytes
        active = copy.deepcopy(self.store.desired_snapshot)
        active["payload"]["repo_defaults_sha256"] = checksum(canonical_bytes(self.store._repo_defaults()))
        active["sha256"] = checksum(canonical_bytes(active["payload"]))
        self.store._write_cache("active.json", active)
        before = (self.store.cache_dir / "active.json").read_bytes()
        self.publish_revision(2)
        with patch.object(self.store, "mark_applied", side_effect=AssertionError("Apply")):
            await self.service.reconcile()
        self.assertEqual(self.store.active_revision, 1)
        self.assertTrue(self.service.source["restart_required"])
        self.assertIsNone(self.service.source["runtime_revision"])
        self.assertEqual((self.store.cache_dir / "active.json").read_bytes(), before)

    async def test_cancelled_edit_owns_blocking_transaction_until_completion(self):
        import threading
        started, finish = threading.Event(), threading.Event()
        def operation():
            started.set()
            finish.wait(3)
            return "finished"
        task = asyncio.create_task(self.service.operation(operation))
        await until(started.is_set)
        task.cancel()
        await asyncio.sleep(0.01)
        self.assertFalse(task.done())
        task.cancel()
        stopping = asyncio.create_task(self.service.stop())
        await asyncio.sleep(0.01)
        self.assertFalse(task.done())
        self.assertFalse(stopping.done())
        finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await stopping


class ControlPlaneApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture, self.store, self.config = make_fixture()
        self.addCleanup(self.fixture.doCleanups)
        self.nats = FakeClient()
        self.app = create_app(self.store, self.config, client_factory=lambda: self.nats)
        self.service = self.app[RECONCILIATION_KEY]
        self.client = TestClient(TestServer(self.app))
        self.addAsyncCleanup(self.client.close)
        await self.client.start_server()
        await until(lambda: self.service.nats_state == "connected")

    async def test_disabled_bank_without_cloud_blobs_can_sync_on_a_fresh_node(self):
        await until(lambda: self.service.soundbank.status["synced_revision"] == 1)
        status = self.service.soundbank.status
        self.assertEqual(status["state"], "disabled")
        self.assertEqual(status["verified_assets"], 0)
        self.assertIsNone(status["error_code"])
        index = json.loads(self.service.soundbank.index.read_bytes())
        self.assertEqual(index["revision"], 1)
        self.assertEqual(index["assets"], 0)
        self.assertFalse((self.store.cache_dir / "active.json").exists())

    async def test_health_is_cheap_structural_snapshot_policy_not_nats_or_runtime_readiness(self):
        with patch.object(self.fixture.drive, "read_manifest", side_effect=AssertionError("Health Drive I/O")), \
                patch.object(self.store, "mark_applied", side_effect=AssertionError("Apply")):
            response = await self.client.get("/healthz")
            self.assertEqual(response.status, 200)
            self.nats.is_connected = False
            await self.service._disconnected()
            self.assertEqual((await self.client.get("/healthz")).status, 200)
            self.service.http_operational = False
            self.assertEqual((await self.client.get("/healthz")).status, 503)
        self.assertIsNone(self.store.runtime_snapshot)

    async def test_safe_routes_assets_and_capabilities_and_runtime_routes_absent(self):
        response = await self.client.get("/settings/")
        self.assertIn('data-settings-mode="control-plane"', await response.text())
        self.assertEqual((await self.client.get("/settings/cluster.js")).status, 200)
        self.assertEqual((await self.client.get("/settings/cluster_logs.js")).status, 200)
        self.assertEqual((await self.client.get("/settings/data.yaml")).status, 404)
        capabilities = await (await self.client.get("/api/settings/capabilities")).json()
        self.assertEqual(capabilities["mode"], "standalone")
        self.assertFalse(capabilities["capabilities"]["rolling_restart"])
        self.assertFalse(capabilities["capabilities"]["memory"])
        self.assertEqual((await self.client.get("/api/settings/memory")).status, 503)
        self.assertTrue(capabilities["capabilities"]["voice_diagnostics"])
        for method, path in (
            ("GET", "/api/settings/status"), ("GET", "/api/settings/logs"),
            ("POST", "/api/settings/restart"),
            ("POST", "/api/settings/push-tts"), ("POST", "/api/settings/source"),
            ("POST", "/api/settings/soundbank/generate"), ("GET", "/xiaozhi/ota/"),
            ("GET", "/mcp/vision/explain"),
        ):
            with self.subTest(path=path):
                self.assertEqual((await self.client.request(method, path)).status, 404)

    async def test_shared_get_put_emits_one_hint_after_cas_and_does_not_apply(self):
        payload = await (await self.client.get("/api/settings")).json()
        self.assertEqual(payload["configuration_source"]["settings_scope"], "cluster")
        self.assertNotIn("private-test-key", json.dumps(payload))
        original_nodes = copy.deepcopy(self.store.desired_snapshot["payload"]["object"]["layers"]["nodes"])
        response = await self.client.put("/api/settings", json={"config": {"prompt": "Shared HTTP edit"}, "base_revision": 1})
        self.assertEqual(response.status, 200)
        await until(lambda: len(self.nats.messages) == 1)
        subject, content = self.nats.messages[0]
        self.assertEqual(subject, CONFIG_CHANGED_SUBJECT)
        self.assertEqual(parse_config_changed(content), 2)
        self.assertEqual(self.fixture.drive.manifest["revision"], 2)
        layers = self.store.desired_snapshot["payload"]["object"]["layers"]
        self.assertEqual(layers["cluster"]["prompt"], "Shared HTTP edit")
        self.assertEqual(layers["nodes"], original_nodes)
        self.assertIsNone(self.store.active_revision)
        self.assertFalse((self.store.cache_dir / "active.json").exists())

    async def test_unrelated_shared_http_save_keeps_cloud_assets_without_local_bytes(self):
        from config.config_store import canonical_bytes, checksum
        obj = copy.deepcopy(self.store.desired_snapshot["payload"]["object"])
        asset_path = self.fixture.directory / "soundbank/hello.wav"
        content = asset_path.read_bytes()
        self.fixture.drive.files["retained-wav"] = content
        soundbank = obj["layers"]["cluster"]["static_soundbank"]
        soundbank["entries"]["Hello"]["cloud"] = {
            "file_id": "retained-wav", "sha256": checksum(content), "size": len(content),
        }
        self.fixture.drive.publish_object(obj, 2)
        await self.service.reconcile()
        # Background synchronization is separate from Save and keeps runtime
        # filenames absent. Wait for it before asserting Save does no blob I/O.
        await until(lambda: self.service.soundbank.status["synced_revision"] == 2)
        asset_path.unlink()
        asset_path.parent.rmdir()
        with patch.object(self.store.soundbank_assets, "publish_layers", side_effect=AssertionError("Asset publication")), \
                patch.object(self.fixture.drive, "upload_blob", side_effect=AssertionError("Blob upload")), \
                patch.object(self.fixture.drive, "download", wraps=self.fixture.drive.download) as download:
            response = await self.client.put("/api/settings", json={"config": {"prompt": "Shared without audio bytes"}, "base_revision": 2})
            self.assertEqual(response.status, 200)
            self.assertNotIn("retained-wav", [call.args[0] for call in download.call_args_list])
        await until(lambda: len(self.nats.messages) == 1)
        self.assertEqual(parse_config_changed(self.nats.messages[0][1]), 3)
        self.assertEqual(self.fixture.drive.manifest["revision"], 3)
        saved = self.store.desired_snapshot["payload"]["object"]
        self.assertEqual(canonical_bytes(saved["layers"]["cluster"]["static_soundbank"]), canonical_bytes(soundbank))
        self.assertFalse(asset_path.parent.exists())
        self.assertIsNone(self.store.active_revision)

    async def test_failed_hint_and_cache_write_keep_successful_cas_successful(self):
        self.nats.fail_publish = True
        writer = self.store._write_cache
        def fail_after_cas(name, envelope):
            if self.fixture.drive.manifest["revision"] > 1:
                raise OSError("private-cache-path")
            writer(name, envelope)
        with patch.object(self.store, "_write_cache", side_effect=fail_after_cas):
            response = await self.client.put("/api/settings", json={"config": {"prompt": "Committed"}, "base_revision": 1})
            self.assertEqual(response.status, 200)
        await until(lambda: self.service.hint_publication["state"] == "failed")
        self.assertEqual(self.fixture.drive.manifest["revision"], 2)
        self.assertEqual(self.store.sync_status, "cache_error")

    async def test_stale_revision_and_failed_cas_emit_no_hint(self):
        self.fixture.drive.before_commit = lambda: setattr(self.fixture.drive, "etag", self.fixture.drive.etag + 1)
        response = await self.client.put("/api/settings", json={"config": {"prompt": "Lost CAS"}, "base_revision": 1})
        self.assertEqual(response.status, 409)
        self.fixture.drive.publish_object(self.store.desired_snapshot["payload"]["object"], 2)
        response = await self.client.put("/api/settings", json={"config": {"prompt": "Stale"}, "base_revision": 1})
        self.assertEqual(response.status, 409)
        self.assertFalse(self.nats.messages)

    async def test_cluster_status_omits_private_metadata_and_raw_errors(self):
        self.store.last_error = "fake-nats-private-password private-reference-name /private/credentials.json"
        await self.service.operation(lambda: None)
        payload = await (await self.client.get("/api/cluster")).json()
        serialized = json.dumps(payload)
        for value in ("fake-nats-private-password", "${secret:", "private-test-key", "private-reference-name",
                      "/private/credentials.json", self.store.manifest_id, self.store.folder_id, "nats://"):
            self.assertNotIn(value, serialized)
        self.assertEqual(payload["ingress"], {"configured_vip": "192.168.1.186", "state": "desired_only"})
        self.assertEqual(payload["node_id"], "test-node")
        self.assertEqual(payload["protocol"], "xiaozhi-control-plane-v1")

    async def test_plaintext_shared_secret_rejected_and_sync_not_apply(self):
        response = await self.client.put("/api/settings", json={"config": {"LLM": {"Test": {"api_key": "new-private"}}}, "base_revision": 1})
        self.assertEqual(response.status, 400)
        self.assertNotIn("new-private", await response.text())
        self.assertEqual(self.fixture.drive.manifest["revision"], 1)
        obj = copy.deepcopy(self.store.desired_snapshot["payload"]["object"])
        self.fixture.drive.publish_object(obj, 2)
        response = await self.client.post("/api/settings/sync", json={})
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["configuration_source"]["desired_revision"], 2)
        self.assertIsNone(self.store.active_revision)

    async def test_legacy_v1_read_save_no_shared_hint_and_explicit_migration_emits_hint(self):
        obj = copy.deepcopy(self.store.desired_snapshot["payload"]["object"])
        obj["schema_version"] = 1
        obj["layers"]["nodes"]["test-node"]["overrides"] = obj["layers"].pop("cluster")
        self.fixture.drive.publish_object(obj, 2)
        await self.service.reconcile()
        response = await self.client.get("/api/settings")
        self.assertEqual((await response.json())["configuration_source"]["settings_scope"], "legacy_node")
        self.assertEqual(self.service.soundbank.status["state"], "legacy")
        response = await self.client.put("/api/settings", json={"config": {"prompt": "Legacy node edit"}, "base_revision": 2})
        self.assertEqual(response.status, 200)
        self.assertFalse(self.nats.messages)
        preview = await self.client.post("/api/settings/migrate-cluster", json={"nodes": ["test-node"]})
        self.assertEqual((await preview.json())["base_revision"], 3)
        apply = await self.client.post("/api/settings/migrate-cluster", json={"nodes": ["test-node"], "apply": True, "base_revision": 3})
        self.assertEqual(apply.status, 200)
        await until(lambda: len(self.nats.messages) == 1)
        self.assertEqual(parse_config_changed(self.nats.messages[0][1]), 4)

    async def test_no_valid_snapshot_or_missing_local_assignment_is_unhealthy(self):
        await self.client.close()
        for missing_node in (False, True):
            with self.subTest(missing_node=missing_node):
                store = self.fixture.new_cloud()
                store.cache_dir = self.fixture.directory / f"fresh-{missing_node}"
                from config.local_config import LocalConfigStore
                store.cache_io = LocalConfigStore(store.cache_dir / ".config.yaml")
                if missing_node:
                    store.bootstrap["node_id"] = "unassigned-node"
                else:
                    self.fixture.drive.offline = True
                app = create_app(store, self.config, client_factory=FakeClient)
                async with TestClient(TestServer(app)) as client:
                    self.assertEqual((await client.get("/healthz")).status, 503)
                self.fixture.drive.offline = False

    async def test_loopback_access_does_not_trust_forwarded_headers(self):
        handler = ControlPlaneSettingsHandler(self.service)
        request = Mock()
        request.transport.get_extra_info.return_value = ("192.168.1.55", 9999)
        request.headers = {"X-Forwarded-For": "127.0.0.1"}
        from aiohttp import web
        with self.assertRaises(web.HTTPForbidden):
            await handler.handle_health(request)

    async def test_offline_startup_uses_validated_desired_cache_but_not_active_only(self):
        await self.client.close()
        self.store._write_cache("active.json", self.store.desired_snapshot)
        self.fixture.drive.offline = True
        for active_only in (False, True):
            with self.subTest(active_only=active_only):
                if active_only:
                    (self.store.cache_dir / "desired.json").unlink()
                store = self.fixture.new_cloud()
                app = create_app(store, self.config, client_factory=FakeClient)
                async with TestClient(TestServer(app)) as client:
                    self.assertEqual((await client.get("/healthz")).status, 503 if active_only else 200)
                    self.assertEqual(app[RECONCILIATION_KEY].reconciliation["state"], "error")


class LightweightAndProtocolTests(unittest.TestCase):
    def test_startup_import_guard_and_no_runtime_activation(self):
        code = '''
import sys, asyncio, importlib.abc
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        forbidden = ("core.providers", "core.connection", "core.websocket_server", "core.http_server",
            "core.api.settings_handler", "core.notification_audio", "core.utils.modules_initialize",
            "core.utils.resource_monitor", "core.utils.ui_log_buffer", "config.logger", "plugins_func",
            "numpy", "torch", "onnxruntime", "opuslib_next", "pydub", "openai", "app")
        if any(fullname == name or fullname.startswith(name + ".") for name in forbidden):
            raise AssertionError("Forbidden import: " + fullname)
sys.meta_path.insert(0, Guard())
sys.path.insert(0, "tests")
import control_plane
from test_control_plane import make_fixture, FakeClient, until
from core.control_plane import create_app, RECONCILIATION_KEY
async def check():
    fixture, store, config = make_fixture()
    try:
        app = create_app(store, config, client_factory=FakeClient)
        app.freeze()
        await app.startup()
        service = app[RECONCILIATION_KEY]
        await until(lambda: service.nats_state == "connected")
        assert service.healthy and service.http_operational
        assert store.runtime_snapshot is None and store.active_revision is None
        await app.shutdown()
        await app.cleanup()
    finally:
        fixture.doCleanups()
asyncio.run(check())
'''
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_shared_connection_config_does_not_read_worker_identity(self):
        with patch.dict(os.environ, {**ENV, "XIAOZHI_WORKER_ID": "invalid.worker.*"}, clear=True):
            self.assertEqual(ControlPlaneConfig.from_env().nats.username, "xiaozhi")
            with self.assertRaises(WorkerConfigError):
                NatsConfig.from_env()

    def test_control_plane_env_defaults_bounds_and_credential_safe_errors(self):
        with patch.dict(os.environ, ENV, clear=True):
            config = ControlPlaneConfig.from_env()
            self.assertEqual((config.host, config.port, config.reconcile_interval), ("127.0.0.1", 8004, 45))
            self.assertNotIn(ENV["XIAOZHI_NATS_PASSWORD"], repr(config))
        for key, value in (("XIAOZHI_CONTROL_PLANE_HOST", "unsafe-private-host"),
                           ("XIAOZHI_CONTROL_PLANE_PORT", "0"), ("XIAOZHI_CONTROL_PLANE_PORT", "65536"),
                           ("XIAOZHI_CONFIG_RECONCILE_SECONDS", "nan"), ("XIAOZHI_CONFIG_RECONCILE_SECONDS", "0"),
                           ("XIAOZHI_CONTROL_PLANE_ALLOW_REMOTE", "maybe"),
                           ("XIAOZHI_NATS_SERVERS", "nats://private-user:private-password@host:4222")):
            with patch.dict(os.environ, {**ENV, key: value}, clear=True):
                with self.assertRaises(ValueError) as error:
                    ControlPlaneConfig.from_env()
                self.assertNotIn(value, str(error.exception))

    def test_protocol_is_fixed_metadata_only_and_rejects_invalid_values(self):
        self.assertEqual(json.loads(config_changed(2)), {"protocol": "xiaozhi-config-v1", "revision": 2})
        self.assertLessEqual(len(config_changed((1 << 63) - 1)), MAX_EVENT_BYTES)
        for value in (True, "2", 0, -1, 1 << 63):
            with self.assertRaises(ValueError):
                config_changed(value)
        self.assertIsNone(parse_config_changed(b'{"protocol":"xiaozhi-config-v1","revision":2,"revision":3}'))

    def test_worker_ping_contract_and_config_still_unchanged(self):
        from core.cluster.protocol import PING_SUBJECT, QUEUE_GROUP, ping_response, targeted_ping_subject
        with patch.dict(os.environ, {**ENV, "XIAOZHI_WORKER_ID": "deskb1x"}, clear=True):
            config = NatsConfig.from_env()
        self.assertEqual(config.worker_id, "deskb1x")
        self.assertEqual(config.servers, NatsConnectionConfig(tuple(ENV["XIAOZHI_NATS_SERVERS"].split(",")), "xiaozhi", "fake").servers)
        self.assertEqual(PING_SUBJECT, "xiaozhi.v1.worker.ping")
        self.assertEqual(QUEUE_GROUP, "xiaozhi-workers")
        self.assertEqual(targeted_ping_subject(config.worker_id), "xiaozhi.v1.worker.deskb1x.ping")
        self.assertEqual(json.loads(ping_response(config.worker_id)), {
            "protocol": "xiaozhi-worker-v1", "worker_id": "deskb1x", "status": "ok", "capabilities": [],
        })
