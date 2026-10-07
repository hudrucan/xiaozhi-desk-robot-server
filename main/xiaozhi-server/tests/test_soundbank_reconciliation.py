"""Three disposable node caches, fake Cloud/NATS; no live services or providers."""

import asyncio
import copy
from dataclasses import replace
import io
import json
import threading
import types
import unittest
from unittest.mock import patch
import wave

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from config.config_store import ConfigUnavailable, checksum
from config.drive_transport import GoogleDriveTransport
from config.google_drive_config import GoogleDriveConfigStore
from core.api.control_plane_settings import ControlPlaneSettingsHandler
from core.cluster.config_reconciliation import ConfigReconciliation, CONTROL_PROTOCOL
from core.cluster.config_protocol import config_changed
from core.cluster.secret_transport import SecretProvisionConfig
from core.utils.config_editor import ConfigEditor
from test_control_plane import FakeClient, make_fixture, until


def audio(value):
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        stream.writeframes(value.to_bytes(2, "little", signed=True) * 1440)
    return output.getvalue()


class SoundbankReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture, _, self.config = make_fixture()
        self.addCleanup(self.fixture.doCleanups)
        self.nodes = ("test-node", "test-node2", "test-node3")
        self.obj = json.loads(self.fixture.drive.files[self.fixture.drive.manifest["config"]["file_id"]])
        assignment = self.obj["layers"]["nodes"]["test-node"]
        for node in self.nodes[1:]:
            self.obj["layers"]["nodes"][node] = copy.deepcopy(assignment)
        for node in self.nodes:
            self.obj["layers"]["nodes"][node]["overrides"]["static_soundbank"] = {
                "directory": str(self.fixture.directory / node / "soundbank")}
        self.set_asset(1, audio(1))
        (self.fixture.directory / "soundbank/hello.wav").unlink()
        self.services = []
        for node in self.nodes:
            bootstrap = copy.deepcopy(self.fixture.bootstrap)
            bootstrap["node_id"] = node
            store = GoogleDriveConfigStore(bootstrap, transport=self.fixture.drive,
                cache_dir=self.fixture.directory / node / "cloud-config",
                default_path=str(self.fixture.default_path), secret_provider=self.fixture.secrets)
            service = ConfigReconciliation(store, replace(self.config, reconcile_interval=0.04),
                                           client_factory=FakeClient)
            self.services.append(service)
            await service.start()
            self.addAsyncCleanup(service.stop)
        await self.ready(1)

    def set_asset(self, revision, content):
        file_id = f"blob-{revision}"
        self.fixture.drive.files[file_id] = content
        bank = self.obj["layers"]["cluster"]["static_soundbank"]
        bank["enabled"] = True
        bank["entries"]["Hello"] = {"file": "hello.wav", "cloud": {
            "file_id": file_id, "size": len(content), "sha256": checksum(content)}}
        self.fixture.drive.publish_object(self.obj, revision)

    async def ready(self, revision):
        await until(lambda: all(service.soundbank.status["state"] == "ready"
            and service.soundbank.status["synced_revision"] == revision for service in self.services))

    def index(self, service):
        return json.loads(service.soundbank.index.read_bytes())

    async def test_startup_verifies_three_complete_caches_without_materializing_runtime(self):
        identities = []
        for service in self.services:
            index = self.index(service)
            pointer = index["configuration"]["static_soundbank"]["entries"]["Hello"]["cloud"]
            identities.append(pointer["sha256"])
            cached = service.soundbank.assets.cache_dir / (pointer["sha256"] + ".wav")
            self.assertEqual(checksum(cached.read_bytes()), pointer["sha256"])
            self.assertEqual(service.soundbank.status["verified_assets"], 1)
            self.assertIsNone(service.store.active_revision)
            self.assertIsNone(service.store.runtime_snapshot)
            self.assertFalse((service.store.cache_dir / "active.json").exists())
            self.assertFalse((self.fixture.directory / service.store.bootstrap["node_id"] / "soundbank").exists())
        self.assertEqual(len(set(identities)), 1)
        self.assertFalse((self.fixture.directory / "soundbank/hello.wav").exists())

    async def test_shared_soundbank_cas_and_hint_update_all_three_caches(self):
        first = self.services[0]
        path = self.fixture.directory / self.nodes[0] / "soundbank/hello.wav"
        path.parent.mkdir()
        changed = audio(2)
        path.write_bytes(changed)
        # Publication validates the shared view before the serving node's
        # directory exception. Author both views; peer runtime paths stay absent.
        (self.fixture.directory / "soundbank/hello.wav").write_bytes(changed)
        result = await first.operation(ConfigEditor(first.store).update,
            {"static_soundbank": {"entries": {"Hello": {"file": "hello.wav"}}}}, base_revision=1)
        self.assertEqual(result["configuration_source"]["desired_revision"], 2)
        await until(lambda: len(first.client.messages) == 1)
        data = first.client.messages[0][1]
        self.assertEqual(data, config_changed(2))
        for service in self.services[1:]:
            await service._on_hint(types.SimpleNamespace(data=data))
        await self.ready(2)
        for service in self.services:
            pointer = self.index(service)["configuration"]["static_soundbank"]["entries"]["Hello"]["cloud"]
            self.assertEqual(pointer["sha256"], checksum(changed))
        for node in self.nodes[1:]:
            self.assertFalse((self.fixture.directory / node / "soundbank").exists())
        self.assertEqual(self.fixture.drive.manifest["revision"], 2)

    async def test_failed_hint_does_not_fail_save_and_local_cache_still_advances(self):
        service = self.services[0]
        service.client.fail_publish = True
        before = self.index(service)["fingerprint"]
        with patch.object(self.fixture.drive, "download", wraps=self.fixture.drive.download) as download:
            await service.operation(ConfigEditor(service.store).update, {"prompt": "Unrelated desired edit"}, base_revision=1)
            await self.ready(2)  # Other nodes self-heal through periodic Cloud refresh.
            self.assertNotIn("blob-1", [call.args[0] for call in download.call_args_list])
        await until(lambda: service.hint_publication["state"] == "failed")
        self.assertEqual(self.index(service)["fingerprint"], before)

    async def test_missing_hint_and_deleted_local_blob_self_heal_periodically(self):
        self.set_asset(2, audio(2))
        await self.ready(2)  # No hint is delivered.
        service = self.services[1]
        pointer = self.index(service)["configuration"]["static_soundbank"]["entries"]["Hello"]["cloud"]
        cached = service.soundbank.assets.cache_dir / (pointer["sha256"] + ".wav")
        cached.unlink()
        await until(cached.exists)
        self.assertEqual(checksum(cached.read_bytes()), pointer["sha256"])

    async def test_corrupt_download_preserves_complete_index_and_retry_recovers(self):
        previous = [service.soundbank.index.read_bytes() for service in self.services]
        content = audio(2)
        self.set_asset(2, content)
        self.fixture.drive.files["blob-2"] = b"corrupt"
        await until(lambda: all(s.soundbank.status["state"] == "error" for s in self.services))
        for service, old in zip(self.services, previous):
            self.assertEqual(service.soundbank.index.read_bytes(), old)
            self.assertEqual(service.soundbank.status["synced_revision"], 1)
            self.assertEqual(service.soundbank.status["error_code"], "soundbank_sync_failed")
            self.assertTrue(service.healthy)
        self.fixture.drive.files["blob-2"] = content
        await self.ready(2)

    async def test_streamed_download_limit_closes_response_without_trusting_headers(self):
        closed = []
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): closed.append(True)
            def iter_content(self, **kwargs):
                yield b"1234"
                yield b"5678"
        transport = GoogleDriveTransport("unused-private-test-credentials.json")
        with patch.object(transport, "_request", return_value=Response()) as request:
            with self.assertRaises(ConfigUnavailable):
                transport.download_limited("fake-blob", 6)
        self.assertEqual(closed, [True])
        self.assertTrue(request.call_args.kwargs["stream"])

    async def test_superseded_transfer_never_publishes_old_pending_generation(self):
        service = self.services[0]
        started, finish = threading.Event(), threading.Event()
        download = service.soundbank.transport.download
        def blocked(file_id):
            if file_id == "blob-2":
                started.set()
                finish.wait(3)
            return download(file_id)
        published = []
        write = service.soundbank.io._atomic_bytes
        def record(path, content):
            published.append(json.loads(content)["revision"])
            write(path, content)
        with patch.object(service.soundbank.transport, "download", side_effect=blocked), \
                patch.object(service.soundbank.io, "_atomic_bytes", side_effect=record):
            try:
                self.set_asset(2, audio(2))
                await until(started.is_set)
                self.set_asset(3, audio(3))
                await service.reconcile()
                await until(lambda: service.soundbank.status["desired_revision"] == 3)
            finally:
                finish.set()
            await self.ready(3)
        self.assertNotIn(2, published)
        self.assertIn(3, published)

    async def test_shutdown_joins_owned_transfer_without_publishing_pending_index(self):
        service = self.services[0]
        started, finish = threading.Event(), threading.Event()
        old = service.soundbank.index.read_bytes()
        download = service.soundbank.transport.download
        def blocked(file_id):
            if file_id == "blob-2":
                started.set()
                finish.wait(3)
            return download(file_id)
        with patch.object(service.soundbank.transport, "download", side_effect=blocked):
            try:
                self.set_asset(2, audio(2))
                await until(started.is_set)
                task = asyncio.create_task(service.stop())
                await until(lambda: service.soundbank.stopping)
                self.assertFalse(task.done())
            finally:
                finish.set()
            await task
        self.assertEqual(service.soundbank.index.read_bytes(), old)

    async def test_peer_status_is_bounded_redacted_and_unavailable_node_is_not_ready(self):
        service = self.services[0]
        service.config = replace(service.config, secrets=SecretProvisionConfig(bytes(32), tuple(
            (node, f"http://10.0.0.{i}:8004") for i, node in enumerate(self.nodes, 1))))
        handler = ControlPlaneSettingsHandler(service)
        app = web.Application()
        app.router.add_get("/api/cluster/soundbank", handler.handle_soundbank_cluster)
        client = TestClient(TestServer(app))
        self.addAsyncCleanup(client.close)
        await client.start_server()
        statuses = {endpoint: other.status() for (_, endpoint), other in zip(service.config.secrets.nodes, self.services)}
        for payload in statuses.values():
            payload["soundbank"]["private_extra"] = "private-credentials-reference-and-path"
        class Content:
            async def iter_chunked(self, size):
                yield self.data
        class Response:
            status = 200
            def __init__(self, data):
                self.content = Content()
                self.content.data = json.dumps(data).encode()
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
        class Session:
            def __init__(self, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def get(self, url, **kwargs):
                if url.startswith("http://10.0.0.3:"):
                    return Response({"protocol": CONTROL_PROTOCOL, "node_id": "wrong-node"})
                return Response(statuses[url.removesuffix("/api/cluster")])
        with patch("core.api.control_plane_settings.ClientSession", Session):
            response = await client.get("/api/cluster/soundbank")
            payload = await response.json()
        self.assertEqual(payload["ready_nodes"], 2)
        self.assertEqual(payload["expected_nodes"], 3)
        self.assertEqual(payload["state"], "pending")
        self.assertEqual(payload["nodes"][2]["state"], "unavailable")
        self.assertNotIn("private-credentials", json.dumps(payload))
        self.assertNotIn("10.0.0", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
