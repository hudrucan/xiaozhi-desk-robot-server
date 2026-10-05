"""Node cloning across two CAS authorities; fake Drive and disposable data only."""

import copy
import json
import unittest
from unittest.mock import patch

from config.bootstrap import load_bootstrap
from config.cloud_clone import (
    CloneNodeConflict, ClonePublicationError, CloneUnsupported, clone_cloud_node, clone_selection,
)
from config.cloud_recovery import RecoveryConflict, RecoveryError, RecoveryIdentityConflict, decrypt_dataset
from config.cloud_restore import RestorePublicationError
from config.config_store import canonical_bytes
import test_cloud_recovery as recovery_tests


class CloudCloneTests(unittest.TestCase):
    def setUp(self):
        self.fixture = recovery_tests.CloudRecoveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.provision()
        self.drive, self.data = self.fixture.drive, self.fixture.data
        self.original_source = copy.deepcopy(self.fixture.source().descriptor)
        self.original_object = self.config_object()
        self.original_memory = self.drive.download(self.original_source["memory_manifest_file_id"])
        self.original_memory_etags = copy.deepcopy(self.drive.memory_etags)
        self.original_revision = self.drive.manifest["revision"]

    def config_object(self):
        return json.loads(self.drive.download(self.drive.manifest["config"]["file_id"]))

    def clone(self, new_node="new-box", passphrase=recovery_tests.PASSPHRASE):
        return clone_cloud_node(self.fixture.source(), "test-node", new_node, passphrase,
            recovery_tests.CREDENTIALS, self.drive, data_dir=self.data, default_path=self.fixture.default_path)

    def assert_no_identity(self):
        self.assertFalse((self.data / "bootstrap.yaml").exists())
        self.assertFalse((self.data / "node-secrets").exists())

    def assert_source_unchanged(self):
        descriptor = self.fixture.source().descriptor
        self.assertEqual(descriptor["nodes"]["test-node"], self.original_source["nodes"]["test-node"])
        obj = self.config_object()
        self.assertEqual(obj["layers"]["nodes"]["test-node"], self.original_object["layers"]["nodes"]["test-node"])
        self.assertEqual(self.drive.download(self.original_source["memory_manifest_file_id"]), self.original_memory)
        self.assertEqual(self.drive.memory_etags, self.original_memory_etags)
        self.assertEqual(json.loads(self.original_memory)["writer_node_id"], "test-node")

    def test_existing_id_in_either_authority_rejected_before_publication(self):
        for node in ("test-node", "reader-node"):
            with self.subTest(node=node):
                uploads = self.drive.uploads
                with self.assertRaises(CloneNodeConflict):
                    self.clone(node)
                self.assertEqual(self.drive.uploads, uploads)
                self.assertEqual(self.drive.manifest["revision"], self.original_revision)
                self.assertEqual(self.fixture.source().descriptor, self.original_source)
                self.assert_no_identity()
        self.assertFalse((self.data / ".cloud-clone/journal.json").exists())

    def test_clone_assignment_secrets_aad_activation_and_source_preservation(self):
        result = self.clone()
        self.assertEqual(result.provider, "google_drive")
        obj, descriptor = self.config_object(), self.fixture.source().descriptor
        self.assertEqual(obj["layers"]["nodes"]["new-box"], self.original_object["layers"]["nodes"]["test-node"])
        for scope in ("global", "environments", "roles"):
            self.assertEqual(obj["layers"][scope], self.original_object["layers"][scope])
        pointer = descriptor["nodes"]["new-box"]["secrets"]
        self.assertNotEqual(pointer, descriptor["nodes"]["test-node"]["secrets"])
        encrypted = self.drive.download(pointer["file_id"])
        source_id = descriptor["source_id"]
        dataset = decrypt_dataset(encrypted, source_id, "new-box", recovery_tests.PASSPHRASE)
        self.assertEqual(dataset, {"node_id": "new-box", "values": self.fixture.secret_store.export_dataset()["values"]})
        with self.assertRaises(RecoveryError):
            decrypt_dataset(encrypted, source_id, "test-node", recovery_tests.PASSPHRASE)
        original = json.loads(self.drive.download(descriptor["nodes"]["test-node"]["secrets"]["file_id"]))
        envelope = json.loads(encrypted)
        self.assertEqual(envelope["node_id"], "new-box")
        self.assertNotEqual(envelope["cipher"]["nonce"], original["cipher"]["nonce"])
        self.assertNotEqual(envelope["kdf"]["salt"], original["kdf"]["salt"])
        bootstrap = load_bootstrap(self.data / "bootstrap.yaml")
        self.assertEqual((bootstrap["node_id"], bootstrap["config_provider"]), ("new-box", "google_drive"))
        self.assertEqual(json.loads(next((self.data / "node-secrets").glob("*.json")).read_bytes()), dataset)
        self.assert_source_unchanged()
        self.fixture.assert_no_runtime_state()
        journal = (self.data / ".cloud-clone/journal.json").read_text()
        for private in [recovery_tests.PASSPHRASE, *dataset["values"].values(), "test-refresh-private"]:
            self.assertNotIn(private, journal)
        for path in (self.data / ".cloud-clone/journal.json", self.data / ".cloud-clone/secrets.enc.json"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_legacy_and_frozen_centralized_sources_reject_clone(self):
        for layers in (
            {"defaults": self.fixture.fixture.fixture.defaults, "overrides": self.fixture.fixture.fixture.cloud_overrides},
            {**self.original_object["layers"], "global": {
                "defaults": self.fixture.fixture.fixture.defaults, "overrides": {}}},
        ):
            with self.subTest(layers=list(layers)):
                self.drive.publish_object({"schema_version": 1, "layers": layers}, self.original_revision + 1)
                uploads = self.drive.uploads
                with self.assertRaises(CloneUnsupported):
                    self.clone()
                self.assertEqual(self.drive.uploads, uploads)
                self.assertEqual(self.fixture.source().descriptor, self.original_source)
                self.assert_no_identity()

    def test_shared_clone_inherits_cluster_and_copies_only_explicit_exception(self):
        from config.cloud_layers import resolve_layers
        from config.config_loader import merge_configs

        obj = self.config_object()
        obj["schema_version"] = 2
        obj["layers"]["cluster"] = obj["layers"]["nodes"]["test-node"]["overrides"]
        obj["layers"]["nodes"]["test-node"]["overrides"] = {"server": {"port": 9002}}
        self.drive.publish_object(obj, self.drive.manifest["revision"] + 1)
        self.original_object = copy.deepcopy(obj)
        self.clone()
        cloned = self.config_object()
        self.assertEqual(cloned["layers"]["cluster"], obj["layers"]["cluster"])
        self.assertEqual(cloned["layers"]["nodes"]["new-box"], obj["layers"]["nodes"]["test-node"])
        self.assertEqual(cloned["layers"]["nodes"]["new-box"]["overrides"], {"server": {"port": 9002}})
        defaults = self.fixture.fixture.fixture.defaults
        self.assertEqual(merge_configs(*resolve_layers(cloned, "new-box", defaults)),
                         merge_configs(*resolve_layers(cloned, "test-node", defaults)))
        self.assert_source_unchanged()
        self.fixture.assert_no_runtime_state()

    def test_config_cas_conflict_resumes_with_new_cas_without_overwriting_source(self):
        self.drive.before_commit = lambda: setattr(self.drive, "etag", self.drive.etag + 1)
        with self.assertRaises(RecoveryConflict):
            self.clone()
        self.assertEqual(self.fixture.source().descriptor, self.original_source)
        self.assertNotIn("new-box", self.config_object()["layers"]["nodes"])
        self.assert_no_identity()
        self.clone()
        self.assert_source_unchanged()

    def test_descriptor_cas_conflict_keeps_config_receipt_and_retry_uploads_nothing(self):
        source = self.fixture.source()
        self.drive.before_descriptor = lambda: self.drive.descriptor_etags.__setitem__(
            source.file_id, self.drive.descriptor_etags[source.file_id] + 1)
        with self.assertRaises(RecoveryConflict):
            self.clone()
        self.assertIn("new-box", self.config_object()["layers"]["nodes"])
        self.assertNotIn("new-box", self.fixture.source().descriptor["nodes"])
        self.assert_no_identity()
        uploads, revision = self.drive.uploads, self.drive.manifest["revision"]
        self.clone()
        self.assertEqual((self.drive.uploads, self.drive.manifest["revision"]), (uploads, revision))
        self.assert_source_unchanged()

    def test_crash_or_lost_response_immediately_after_config_cas_is_idempotent(self):
        def crash():
            raise OSError(recovery_tests.PRIVATE)

        self.drive.after_config = crash
        with self.assertRaises(ClonePublicationError) as raised:
            self.clone()
        self.assertNotIn(recovery_tests.PRIVATE, str(raised.exception))
        self.assertTrue(raised.exception.__suppress_context__)
        value = json.loads((self.data / ".cloud-clone/journal.json").read_bytes())
        self.assertFalse(value["config_published"])
        self.assertIn("new-box", self.config_object()["layers"]["nodes"])
        self.assertNotIn("new-box", self.fixture.source().descriptor["nodes"])
        self.assert_no_identity()
        self.drive.after_config = None
        uploads, revision = self.drive.uploads, self.drive.manifest["revision"]
        self.clone()
        self.assertEqual((self.drive.uploads, self.drive.manifest["revision"]), (uploads, revision))
        self.assert_source_unchanged()

    def test_descriptor_only_publication_is_also_resumable(self):
        from config.cloud_clone import _publish_descriptor

        def interrupt(journal, value, store, target_store):
            _publish_descriptor(journal, value, self.drive)
            raise OSError(recovery_tests.PRIVATE)

        with patch("config.cloud_clone._publish_config", side_effect=interrupt):
            with self.assertRaises(ClonePublicationError):
                self.clone()
        self.assertIn("new-box", self.fixture.source().descriptor["nodes"])
        self.assertNotIn("new-box", self.config_object()["layers"]["nodes"])
        self.assert_no_identity()
        self.clone()
        self.assert_source_unchanged()

    def test_lost_descriptor_response_and_completed_retry_do_not_duplicate_publications(self):
        replace = self.drive.replace_manifest
        source = self.fixture.source()

        def lost_response(file_id, content, etag):
            replace(file_id, content, etag)
            if file_id == source.file_id:
                raise OSError(recovery_tests.PRIVATE)

        with patch.object(self.drive, "replace_manifest", side_effect=lost_response):
            with self.assertRaises(ClonePublicationError):
                self.clone()
        self.assert_no_identity()
        uploads, revision, updates = self.drive.uploads, self.drive.manifest["revision"], self.drive.descriptor_updates
        self.clone()
        self.clone()
        self.assertEqual((self.drive.uploads, self.drive.manifest["revision"], self.drive.descriptor_updates),
                         (uploads, revision, updates))
        self.assert_source_unchanged()

    def test_unrelated_config_edits_between_authorities_survive_retry(self):
        with patch("config.cloud_clone._publish_descriptor", side_effect=OSError(recovery_tests.PRIVATE)):
            with self.assertRaises(ClonePublicationError):
                self.clone()
        changed = self.config_object()
        changed["layers"]["nodes"]["reader-node"]["overrides"]["server"]["port"] = 8123
        self.drive.publish_object(changed, self.drive.manifest["revision"] + 1)
        self.clone()
        self.assertEqual(self.config_object(), changed)
        self.assert_source_unchanged()

    def test_equal_assignment_from_competing_creator_does_not_prove_our_cas(self):
        def compete():
            obj = copy.deepcopy(self.original_object)
            obj["layers"]["nodes"]["new-box"] = copy.deepcopy(obj["layers"]["nodes"]["test-node"])
            self.drive.publish_object(obj, self.original_revision + 1)

        self.drive.before_commit = compete
        with self.assertRaises(RecoveryConflict):
            self.clone()
        with self.assertRaises(CloneNodeConflict):
            self.clone()
        self.assert_no_identity()
        self.assertEqual(self.fixture.source().descriptor, self.original_source)

    def test_local_publication_failure_resumes_as_new_identity_with_cloud_unchanged(self):
        from config.cloud_restore import _atomic

        def fail_secret(path, content):
            if path.parent.name == "node-secrets":
                raise OSError(recovery_tests.PRIVATE)
            return _atomic(path, content)

        with patch("config.cloud_restore._atomic", side_effect=fail_secret):
            with self.assertRaises(RestorePublicationError):
                self.clone()
        self.assertFalse((self.data / "bootstrap.yaml").exists())
        self.assertTrue((self.data / ".cloud-recovery/journal.json").exists())
        uploads, revision = self.drive.uploads, self.drive.manifest["revision"]
        self.clone()
        self.assertEqual((self.drive.uploads, self.drive.manifest["revision"]), (uploads, revision))
        self.assertEqual(load_bootstrap(self.data / "bootstrap.yaml")["node_id"], "new-box")

    def test_resume_requires_same_identity_passphrase_and_intact_ciphertext(self):
        with patch("config.cloud_clone._publish_descriptor", side_effect=OSError(recovery_tests.PRIVATE)):
            with self.assertRaises(ClonePublicationError):
                self.clone()
        self.assertEqual(clone_selection(self.data), {"source_id": self.original_source["source_id"],
            "source_node_id": "test-node", "new_node_id": "new-box"})
        uploads = self.drive.uploads
        with self.assertRaises(RecoveryIdentityConflict):
            self.clone("different-box")
        with self.assertRaises(RecoveryError):
            self.clone(passphrase="wrong")
        (self.data / ".cloud-clone/secrets.enc.json").write_bytes(b"corrupted")
        with self.assertRaises(RecoveryError):
            self.clone()
        self.assertEqual(self.drive.uploads, uploads)
        self.assert_no_identity()

    def test_wrong_passphrase_and_failed_durable_intent_never_publish(self):
        uploads = self.drive.uploads
        with self.assertRaises(RecoveryError):
            self.clone(passphrase="wrong")
        with patch("config.cloud_clone._CloneJournal.write", side_effect=OSError(recovery_tests.PRIVATE)):
            with self.assertRaises(ClonePublicationError):
                self.clone()
        self.assertEqual(self.drive.uploads, uploads)
        self.assertEqual(self.drive.manifest["revision"], self.original_revision)
        self.assertEqual(self.fixture.source().descriptor, self.original_source)
        self.assert_no_identity()

    def test_config_verification_after_publication_blocks_changed_assignment(self):
        from config.cloud_clone import _publish_descriptor

        def change_config(journal, value, transport):
            _publish_descriptor(journal, value, transport)
            obj = self.config_object()
            obj["layers"]["nodes"]["new-box"]["environment"] = None
            self.drive.publish_object(obj, self.drive.manifest["revision"] + 1)

        with patch("config.cloud_clone._publish_descriptor", side_effect=change_config):
            with self.assertRaises(CloneNodeConflict):
                self.clone()
        self.assert_no_identity()

    def test_journal_parent_sync_failure_prevents_any_cloud_publication(self):
        from config.local_config import LocalConfigStore
        sync = LocalConfigStore._sync_directory

        def fail_parent(path):
            if path == self.data and (self.data / ".cloud-clone/journal.json").exists():
                raise OSError(recovery_tests.PRIVATE)
            return sync(path)

        uploads = self.drive.uploads
        with patch("config.cloud_clone.LocalConfigStore._sync_directory", side_effect=fail_parent):
            with self.assertRaises(ClonePublicationError):
                self.clone()
        self.assertEqual(self.drive.uploads, uploads)
        self.assertEqual(self.drive.manifest["revision"], self.original_revision)
        self.assertEqual(self.fixture.source().descriptor, self.original_source)
        self.assert_no_identity()
        self.clone()
        self.assert_source_unchanged()

    def test_descriptor_verification_after_publication_blocks_wrong_backup_pointer(self):
        from config.cloud_clone import _publish_descriptor

        def change_descriptor(journal, value, transport):
            _publish_descriptor(journal, value, transport)
            source = self.fixture.source()
            descriptor = copy.deepcopy(source.descriptor)
            descriptor["nodes"]["new-box"] = copy.deepcopy(descriptor["nodes"]["test-node"])
            self.drive.replace_manifest(source.file_id, canonical_bytes(descriptor), source.etag)

        with patch("config.cloud_clone._publish_descriptor", side_effect=change_descriptor):
            with self.assertRaises(RecoveryConflict):
                self.clone()
        self.assert_no_identity()
