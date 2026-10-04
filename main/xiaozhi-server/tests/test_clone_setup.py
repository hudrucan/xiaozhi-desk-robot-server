"""First-run clone API, resume metadata and preservation of existing restore."""

import unittest
from unittest.mock import Mock, patch

import yaml

from config.cloud_clone import clone_cloud_node
from config.first_run import FirstRunError
import test_cloud_recovery as recovery_tests
import test_first_run_setup as setup_tests


class CloneSetupTests(setup_tests.SetupAPIFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.fixture = recovery_tests.CloudRecoveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.provision()
        self.authorize_files()
        self.handler.transport_factory = Mock(return_value=self.fixture.drive)
        self.handler.restorer = Mock(side_effect=AssertionError("Clone must not restore the source identity"))

        def clone(*args, **kwargs):
            return clone_cloud_node(*args, **kwargs, default_path=self.fixture.default_path)

        self.handler.cloner = Mock(side_effect=clone)
        self.body = {"source_id": self.fixture.source().descriptor["source_id"], "node_id": "test-node",
                     "mode": "clone", "new_node_id": "new-box", "passphrase": recovery_tests.PASSPHRASE}

    async def test_clone_ui_and_api_activate_only_new_identity(self):
        html = await (await self.client.get("/setup/")).text()
        for text in ("Restore existing node", "Create a new node from selected backup", "New node ID",
                     "provider credentials and secrets", "Memory retains its existing writer"):
            self.assertIn(text, html)
        response = await self.post("restore", json=self.body)
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["provider"], "google_drive")
        self.handler.restorer.assert_not_called()
        self.assertEqual(self.handler.cloner.call_args.args[1:3], ("test-node", "new-box"))
        self.assertEqual(yaml.safe_load((self.data / "bootstrap.yaml").read_bytes())["node_id"], "new-box")
        self.assertFalse((self.data / ".first-run").exists())
        self.assertEqual((await self.post("restart")).status, 200)

    async def test_existing_and_invalid_ids_return_controlled_errors(self):
        uploads = self.fixture.drive.uploads
        for node in ("test-node", "reader-node"):
            response = await self.post("restore", json={**self.body, "new_node_id": node})
            self.assertEqual(response.status, 409)
        for node in ("../../private", "two boxes", "", "line\nbreak"):
            response = await self.post("restore", json={**self.body, "new_node_id": node})
            self.assertEqual(response.status, 400)
        self.assertEqual(self.fixture.drive.uploads, uploads)
        self.assertFalse((self.data / "bootstrap.yaml").exists())
        self.assertTrue((self.data / ".first-run").exists())

    async def test_partial_clone_returns_safe_resume_metadata_and_retries(self):
        with patch("config.cloud_clone._publish_descriptor", side_effect=OSError(recovery_tests.PRIVATE)):
            response = await self.post("restore", json=self.body)
        self.assertEqual(response.status, 503)
        text = await response.text()
        for private in (recovery_tests.PRIVATE, recovery_tests.PASSPHRASE, "test-refresh-private", str(self.data)):
            self.assertNotIn(private, text)
        status = await (await self.client.get("/api/setup/status")).json()
        self.assertEqual(status["clone_selection"], {"source_id": self.body["source_id"],
            "source_node_id": "test-node", "new_node_id": "new-box"})
        # Choosing existing-node recovery cannot abandon an unfinished clone.
        existing = {key: self.body[key] for key in ("source_id", "node_id", "passphrase")}
        self.assertEqual((await self.post("restore", json=existing)).status, 503)
        self.handler.restorer.assert_not_called()
        revision, uploads = self.fixture.drive.manifest["revision"], self.fixture.drive.uploads
        self.assertEqual((await self.post("restore", json=self.body)).status, 200)
        self.assertEqual((self.fixture.drive.manifest["revision"], self.fixture.drive.uploads), (revision, uploads))

    async def test_failed_marker_removal_retries_completed_clone_without_duplicate_cas(self):
        with patch("core.api.setup_handler.finish_first_run", side_effect=FirstRunError()):
            response = await self.post("restore", json=self.body)
        self.assertEqual(response.status, 503)
        self.assertTrue((self.data / ".first-run").exists())
        revision, uploads = self.fixture.drive.manifest["revision"], self.fixture.drive.uploads
        self.assertEqual((await self.post("restore", json=self.body)).status, 200)
        self.assertEqual((self.fixture.drive.manifest["revision"], self.fixture.drive.uploads), (revision, uploads))
        self.assertFalse((self.data / ".first-run").exists())

    async def test_status_does_not_import_restore_or_runtime_modules(self):
        original = __import__

        def checked(name, *args, **kwargs):
            self.assertNotIn(name, {"config.cloud_restore", "config.google_drive_config", "core.soundbank", "core.websocket_server"})
            return original(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=checked):
            response = await self.client.get("/api/setup/status")
        self.assertEqual(response.status, 200)
        self.assertIsNone((await response.json())["clone_selection"])
