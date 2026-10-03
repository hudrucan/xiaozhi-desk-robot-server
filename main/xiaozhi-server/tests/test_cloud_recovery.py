"""Encrypted recovery/discovery and format-box simulation; fake Drive only."""

import copy
import errno
import importlib.util
import io
import json
import logging
import shutil
import tempfile
import unittest
import uuid
import warnings
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import yaml
from google.oauth2.credentials import Credentials

from config.bootstrap import load_bootstrap
from config.cloud_provisioning import ProvisioningError, provision_cloud_state
from config.cloud_recovery import (
    DiscoveredSource, RecoveryConflict, RecoveryError, RecoveryIdentityConflict,
    backup_cloud_node, decrypt_dataset, discover_sources, encrypt_dataset, fetch_backup, publish_recovery_descriptor,
)
from config.cloud_restore import RestorePublicationError, restore_cloud_node, select_node, select_source
from config.config_store import ConfigConflict, canonical_bytes, checksum
from config.drive_transport import GoogleDriveTransport
from config.recovery_cli import recovery_passphrase
from config.recovery_oauth import (
    OAuthClientError, OAuthPortError, OAuthStorageError, browser_available,
    install_oauth_file, load_oauth_client, obtain_credentials,
)
import test_cloud_provisioning as provisioning_tests

PASSPHRASE = "a long test recovery passphrase"
PRIVATE = "secret-token /private/credentials.json api-value"
CREDENTIALS = canonical_bytes({"type": "authorized_user", "client_id": "test-client",
                              "client_secret": "client-private", "refresh_token": "test-refresh-private"})
CLIENT = {"installed": {"client_id": "test-client", "client_secret": "client-private",
                       "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                       "token_uri": "https://oauth2.googleapis.com/token"}}


class RecoveryDrive(provisioning_tests.ProvisionDrive):
    def __init__(self, defaults, overrides):
        super().__init__(defaults, overrides)
        self.markers = []
        self.descriptor_etags = {}
        self.before_descriptor = None
        self.descriptor_updates = 0
        self.lost_create_response = False

    def list_state_descriptors(self):
        return copy.deepcopy(self.markers)

    def create_state_descriptor(self, folder_id, content, source_id):
        file_id = self.upload_immutable(folder_id, content, "cloud-state.json")
        self.markers.append({"file_id": file_id, "source_id": source_id})
        self.descriptor_etags[file_id] = 1
        if self.lost_create_response:
            self.lost_create_response = False
            raise OSError(PRIVATE)
        return file_id

    def read_manifest(self, file_id):
        if file_id in self.descriptor_etags:
            return self.download(file_id), str(self.descriptor_etags[file_id])
        return super().read_manifest(file_id)

    def replace_manifest(self, file_id, content, etag):
        if file_id not in self.descriptor_etags:
            return super().replace_manifest(file_id, content, etag)
        if self.before_descriptor:
            callback, self.before_descriptor = self.before_descriptor, None
            callback()
        if etag != str(self.descriptor_etags[file_id]):
            raise ConfigConflict(PRIVATE)
        self.files[file_id] = content
        self.descriptor_etags[file_id] += 1
        self.descriptor_updates += 1


class CloudRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = provisioning_tests.FullStateProvisioningTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.drive = RecoveryDrive(self.fixture.fixture.defaults, self.fixture.fixture.cloud_overrides)
        self.drive.publish_object(self.fixture.obj, 4)
        self.fixture.drive = self.drive
        self.secret_store = self.fixture.fixture.secrets
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.box = Path(self.temporary.name)
        self.data = self.box / "data"
        defaults = copy.deepcopy(self.fixture.fixture.defaults)
        defaults["static_soundbank"]["directory"] = str(self.data / "soundbank")
        defaults["Memory"]["ExplicitAlias"]["path"] = str(self.data / ".memory.yaml")
        self.default_path = self.box / "config.yaml"
        self.default_path.write_text(yaml.safe_dump(defaults))

    def provision(self, **kwargs):
        return provision_cloud_state(self.fixture.local, bootstrap_path=self.fixture.bootstrap_path,
            transport=self.drive, secret_provider=self.secret_store, receipt_path=self.fixture.receipt_path,
            runtime_cache_dir=self.fixture.cache_dir, recovery_passphrase=PASSPHRASE, **kwargs)

    def source(self):
        return select_source(discover_sources(self.drive))

    def restore(self, *, source=None, node=None, passphrase=PASSPHRASE, activate=True):
        source = source or self.source()
        return restore_cloud_node(source, node or "test-node", passphrase, CREDENTIALS, self.drive,
                                  data_dir=self.data, default_path=self.default_path, activate=activate)

    def assert_no_runtime_state(self):
        for name in (".memory.yaml", "soundbank", "cloud-config", "cloud-memory", "cloud-soundbank"):
            self.assertFalse((self.data / name).exists(), name)

    def test_descriptor_after_full_provisioning_exact_authorities_local_provider(self):
        result = self.provision(source_label="Test desk robot")
        source = self.source()
        bootstrap = load_bootstrap(self.fixture.bootstrap_path)
        self.assertEqual(result.descriptor_file_id, source.file_id)
        self.assertEqual(source.descriptor["label"], "Test desk robot")
        self.assertEqual(source.descriptor["folder_id"], bootstrap["google_drive"]["folder_id"])
        self.assertEqual(source.descriptor["config_manifest_file_id"], bootstrap["google_drive"]["manifest_file_id"])
        self.assertEqual(source.descriptor["memory_manifest_file_id"], bootstrap["google_drive"]["memory_manifest_file_id"])
        self.assertEqual(bootstrap["config_provider"], "local")
        self.assertEqual(source.metadata()["nodes"], ["test-node"])
        self.assertEqual(set(source.metadata()), {"source_id", "label", "nodes"})

    def test_identical_rerun_reuses_descriptor_backup_and_authorities(self):
        self.provision()
        before = self.drive.uploads, copy.deepcopy(self.drive.files), self.drive.descriptor_updates
        self.provision()
        self.assertEqual((self.drive.uploads, self.drive.files, self.drive.descriptor_updates), before)
        self.assertEqual(len(self.drive.markers), 1)

    def test_lost_initial_descriptor_response_is_discovered_on_rerun(self):
        self.drive.lost_create_response = True
        before = self.fixture.bootstrap_path.read_bytes()
        with self.assertRaises(ProvisioningError):
            self.provision()
        self.assertEqual(self.fixture.bootstrap_path.read_bytes(), before)
        self.provision()
        self.assertEqual(len(self.drive.markers), 1)

    def test_descriptor_cas_race_rejects_without_overwriting_winner(self):
        self.provision()
        source = self.source()
        self.secret_store.put_many({"NEW_REF": "new private value"})
        winner = []

        def race():
            descriptor = copy.deepcopy(source.descriptor)
            descriptor["label"] = "Concurrent label"
            self.drive.files[source.file_id] = canonical_bytes(descriptor)
            self.drive.descriptor_etags[source.file_id] += 1
            winner.append(self.drive.files[source.file_id])

        self.drive.before_descriptor = race
        with self.assertRaises(RecoveryConflict):
            publish_recovery_descriptor(load_bootstrap(self.fixture.bootstrap_path), self.drive, self.secret_store, PASSPHRASE)
        self.assertEqual(self.drive.files[source.file_id], winner[0])
        self.assertEqual(self.drive.descriptor_updates, 0)

    def test_multiple_sources_discovered_by_marker_without_known_ids(self):
        self.provision()
        descriptor = copy.deepcopy(self.source().descriptor)
        descriptor.update(source_id=str(uuid.uuid4()), label="Second source", folder_id="second-folder",
                          config_manifest_file_id="second-config")
        self.drive.create_state_descriptor("second-folder", canonical_bytes(descriptor), descriptor["source_id"])
        sources = discover_sources(self.drive)
        self.assertEqual(len(sources), 2)
        with self.assertRaises(RecoveryError):
            select_source(sources)
        self.assertEqual(select_source(sources, descriptor["source_id"]).descriptor["label"], "Second source")

    def test_multiple_nodes_require_selection_single_node_auto_selects(self):
        self.provision()
        source = self.source()
        self.assertEqual(select_node(source), "test-node")
        descriptor = copy.deepcopy(source.descriptor)
        descriptor["nodes"]["reader-node"] = copy.deepcopy(descriptor["nodes"]["test-node"])
        two_nodes = DiscoveredSource(source.file_id, source.etag, descriptor)
        with self.assertRaises(RecoveryError):
            select_node(two_nodes)
        self.assertEqual(select_node(two_nodes, "reader-node"), "reader-node")
        with self.assertRaises(RecoveryError):
            select_node(two_nodes, "missing-node")

    def test_ciphertext_no_plaintext_or_reference_payload_and_exact_restore(self):
        self.provision()
        source = self.source()
        pointer = source.descriptor["nodes"]["test-node"]["secrets"]
        content = self.drive.files[pointer["file_id"]]
        dataset = self.secret_store.export_dataset()
        for name, value in dataset["values"].items():
            self.assertNotIn(name.encode(), content)
            self.assertNotIn(value.encode(), content)
        self.assertNotIn(PASSPHRASE.encode(), content)
        self.assertEqual(fetch_backup(self.drive, pointer, source.descriptor["source_id"], "test-node", PASSPHRASE), dataset)
        self.restore()
        restored = json.loads(next((self.data / "node-secrets").glob("*.json")).read_bytes())
        self.assertEqual(restored, dataset)

    def test_wrong_passphrase_or_hash_never_publishes_local_identity(self):
        self.provision()
        with self.assertRaises(RecoveryError):
            self.restore(passphrase="incorrect passphrase")
        self.assertFalse(self.data.exists())
        source = self.source()
        pointer = source.descriptor["nodes"]["test-node"]["secrets"]
        self.drive.files[pointer["file_id"]] += b"tampered"
        with self.assertRaises(RecoveryError):
            self.restore()
        self.assertFalse(self.data.exists())

    def test_wrong_source_node_aad_or_tampered_ciphertext_fails(self):
        self.provision()
        source = self.source()
        content = self.drive.files[source.descriptor["nodes"]["test-node"]["secrets"]["file_id"]]
        for source_id, node in ((str(uuid.uuid4()), "test-node"), (source.descriptor["source_id"], "reader-node")):
            with self.assertRaises(RecoveryError):
                decrypt_dataset(content, source_id, node, PASSPHRASE)
            # Matching forged headers must still fail authenticated AAD, not
            # merely the envelope's structural identity checks.
            transplanted = json.loads(content)
            transplanted["source_id"], transplanted["node_id"] = source_id, node
            with self.assertRaises(RecoveryError):
                decrypt_dataset(canonical_bytes(transplanted), source_id, node, PASSPHRASE)
        envelope = json.loads(content)
        envelope["cipher"]["ciphertext"] = "A" + envelope["cipher"]["ciphertext"][1:]
        with self.assertRaises(RecoveryError):
            decrypt_dataset(canonical_bytes(envelope), source.descriptor["source_id"], "test-node", PASSPHRASE)

    def test_invalid_kdf_metadata_rejected_before_key_derivation(self):
        self.provision()
        source = self.source()
        envelope = json.loads(self.drive.files[source.descriptor["nodes"]["test-node"]["secrets"]["file_id"]])
        envelope["kdf"]["n"] = 2 ** 60
        with patch("config.cloud_recovery._key", side_effect=AssertionError("Unbounded KDF")):
            with self.assertRaises(RecoveryError):
                decrypt_dataset(canonical_bytes(envelope), source.descriptor["source_id"], "test-node", PASSPHRASE)

    def test_duplicate_secret_store_fields_reject_backup_without_descriptor_mutation(self):
        self.provision()
        before = copy.deepcopy(self.source().descriptor), self.drive.uploads
        self.secret_store.path.write_bytes(b'{"node_id":"test-node","values":{},"values":{}}')
        with self.assertRaises(RecoveryError):
            publish_recovery_descriptor(load_bootstrap(self.fixture.bootstrap_path), self.drive, self.secret_store, PASSPHRASE)
        self.assertEqual((self.source().descriptor, self.drive.uploads), before)

    def test_changed_secrets_create_new_immutable_backup_and_cas_pointer(self):
        self.provision()
        original = self.source().descriptor["nodes"]["test-node"]["secrets"]
        previous_bytes = self.drive.files[original["file_id"]]
        self.secret_store.put_many({"NEW_REF": "replacement private secret"})
        self.provision()
        updated = self.source().descriptor["nodes"]["test-node"]["secrets"]
        self.assertNotEqual(updated["file_id"], original["file_id"])
        self.assertEqual(self.drive.files[original["file_id"]], previous_bytes)
        self.assertEqual(self.drive.descriptor_updates, 1)
        self.assertEqual(fetch_backup(self.drive, updated, self.source().descriptor["source_id"], "test-node", PASSPHRASE), self.secret_store.export_dataset())

    def test_rotation_is_explicit_and_preserves_secret_names(self):
        self.provision()
        bootstrap = load_bootstrap(self.fixture.bootstrap_path)
        with self.assertRaises(RecoveryError):
            publish_recovery_descriptor(bootstrap, self.drive, self.secret_store, "new passphrase")
        publish_recovery_descriptor(bootstrap, self.drive, self.secret_store, "new passphrase", rotate=True)
        source = self.source()
        restored = fetch_backup(self.drive, source.descriptor["nodes"]["test-node"]["secrets"],
                               source.descriptor["source_id"], "test-node", "new passphrase")
        self.assertEqual(restored, self.secret_store.export_dataset())

    def test_existing_cloud_node_backup_updates_only_recovery_state(self):
        self.provision()
        bootstrap = load_bootstrap(self.fixture.bootstrap_path)
        bootstrap["config_provider"] = "google_drive"
        before = copy.deepcopy(self.drive.manifest), self.fixture.local_files(), self.fixture.bootstrap_path.read_bytes()
        self.secret_store.put_many({"CLOUD_SAVE_REF": "new cloud-node secret"})
        config_revision, memory_revision, identifier = backup_cloud_node(bootstrap, PASSPHRASE,
            transport=self.drive, secrets=self.secret_store, cache_dir=self.fixture.cache_dir,
            default_path=self.fixture.fixture.default_path)
        self.assertEqual(config_revision, 5)
        self.assertEqual(memory_revision, 1)
        self.assertEqual(identifier, self.source().file_id)
        self.assertEqual((self.drive.manifest, self.fixture.local_files(), self.fixture.bootstrap_path.read_bytes()), before)
        self.assertFalse(self.fixture.cache_dir.exists())

    def test_nonexplicit_node_backup_preserves_shared_memory_and_other_node_bundle(self):
        from config.cloud_secrets import LocalSecretStore
        from test_cloud_soundbank import wav_bytes
        self.provision()
        original = copy.deepcopy(self.source().descriptor)
        obj = self.fixture.current_object()
        reader = obj["layers"]["nodes"]["reader-node"]["overrides"]
        reader["selected_module"] = {"Memory": "Disabled"}
        reader["Memory"] = {"Disabled": {"type": "nomem"}}
        content = wav_bytes(7)
        reader_pointer = {"file_id": "reader-asset", "sha256": checksum(content), "size": len(content)}
        self.drive.files["reader-asset"] = content
        reader["static_soundbank"]["entries"]["Shared"]["cloud"] = reader_pointer
        reader["static_soundbank"]["entries"]["Repo only"] = copy.deepcopy(
            obj["layers"]["nodes"]["test-node"]["overrides"]["static_soundbank"]["entries"]["Repo only"])
        self.drive.publish_object(obj, 6)
        secrets = LocalSecretStore("reader-node", self.box / "reader-secrets")
        secrets.put_many({"READER_KEY": "reader private key"})
        bootstrap = load_bootstrap(self.fixture.bootstrap_path)
        bootstrap["node_id"] = "reader-node"
        bootstrap["google_drive"].pop("memory_manifest_file_id")
        backup_cloud_node(bootstrap, PASSPHRASE, transport=self.drive, secrets=secrets,
            default_path=self.fixture.fixture.default_path, cache_dir=self.fixture.cache_dir)
        updated = self.source()
        self.assertEqual(updated.descriptor["memory_manifest_file_id"], original["memory_manifest_file_id"])
        self.assertEqual(updated.descriptor["nodes"]["test-node"], original["nodes"]["test-node"])
        self.assertEqual(select_node(updated, "reader-node"), "reader-node")
        restored = self.restore(source=updated, node="reader-node")
        self.assertIsNone(restored.memory_revision)
        self.assertEqual(json.loads(next((self.data / "node-secrets").glob("*.json")).read_bytes()), secrets.export_dataset())

    def test_corrupt_remote_asset_or_missing_memory_rejects_before_publication(self):
        self.provision()
        source = self.source()
        obj = self.fixture.current_object()
        pointer = obj["layers"]["nodes"]["test-node"]["overrides"]["static_soundbank"]["entries"]["Hello"]["cloud"]
        content = self.drive.files[pointer["file_id"]]
        self.drive.files[pointer["file_id"]] = b"corrupt remote asset"
        with self.assertRaises(RecoveryError):
            self.restore()
        self.assertFalse(self.data.exists())
        self.drive.files[pointer["file_id"]] = content
        del self.drive.files[source.descriptor["memory_manifest_file_id"]]
        with self.assertRaises(RecoveryError):
            self.restore()
        self.assertFalse(self.data.exists())

    def test_unresolved_descriptor_creation_intent_cannot_create_another_source(self):
        self.provision()
        self.drive.markers = []  # Missing/invisible does not authorize replacement.
        before = len(self.drive.descriptor_etags)
        with self.assertRaises(ProvisioningError):
            self.provision()
        self.assertEqual(len(self.drive.descriptor_etags), before)

    def test_readiness_is_read_only_and_activation_requires_explicit_request(self):
        self.provision()
        with patch("config.cloud_soundbank.CloudSoundbankAssets.materialize", side_effect=AssertionError()), \
                patch("config.cloud_memory.CloudMemoryStore.sync", side_effect=AssertionError()), \
                patch("config.google_drive_config.GoogleDriveConfigStore.prepare_runtime", side_effect=AssertionError()), \
                patch("config.google_drive_config.GoogleDriveConfigStore.mark_applied", side_effect=AssertionError()):
            result = self.restore(activate=False)
        self.assertEqual(result.provider, "local")
        self.assertEqual(load_bootstrap(self.data / "bootstrap.yaml")["config_provider"], "local")
        self.assert_no_runtime_state()
        self.assertEqual(self.restore(activate=True).provider, "google_drive")

    def test_explicit_memory_and_bootstrap_ids_reconstructed_automatically(self):
        self.provision()
        result = self.restore()
        bootstrap = load_bootstrap(self.data / "bootstrap.yaml")
        descriptor = self.source().descriptor
        self.assertEqual(result.memory_revision, 1)
        self.assertEqual(bootstrap["node_id"], "test-node")
        self.assertEqual(bootstrap["google_drive"]["manifest_file_id"], descriptor["config_manifest_file_id"])
        self.assertEqual(bootstrap["google_drive"]["memory_manifest_file_id"], descriptor["memory_manifest_file_id"])
        self.assertEqual(bootstrap["google_drive"]["folder_id"], descriptor["folder_id"])
        self.assert_no_runtime_state()

    def test_nonexplicit_memory_has_no_memory_authority_requirement(self):
        overrides = copy.deepcopy(self.fixture.fixture.overrides)
        overrides["selected_module"] = {"Memory": "Disabled"}
        overrides["Memory"] = {"Disabled": {"type": "nomem"}}
        with self.fixture.local.locked():
            self.fixture.local.commit_unlocked(overrides)
        self.provision()
        self.assertIsNone(self.source().descriptor["memory_manifest_file_id"])
        result = self.restore()
        self.assertIsNone(result.memory_revision)
        self.assertNotIn("memory_manifest_file_id", load_bootstrap(self.data / "bootstrap.yaml")["google_drive"])

    def test_restore_interruption_journal_bootstrap_last_and_resume(self):
        self.provision()
        from config.cloud_restore import _atomic

        def fail_secret(path, content):
            if path.parent.name == "node-secrets":
                raise OSError(PRIVATE)
            return _atomic(path, content)

        with patch("config.cloud_restore._atomic", side_effect=fail_secret):
            with self.assertRaises(RestorePublicationError):
                self.restore()
        self.assertFalse((self.data / "bootstrap.yaml").exists())
        self.assertTrue((self.data / ".cloud-recovery/journal.json").exists())
        self.assertTrue((self.data / "drive-credentials.json").exists())
        self.restore()
        self.assertEqual(load_bootstrap(self.data / "bootstrap.yaml")["config_provider"], "google_drive")
        self.assertFalse((self.data / ".cloud-recovery").exists())
        self.assert_no_runtime_state()

    def test_existing_mismatched_bootstrap_or_secret_identity_rejected(self):
        self.provision()
        self.restore()
        before = {str(path): path.read_bytes() for path in self.data.rglob("*") if path.is_file()}
        with self.assertRaises(RecoveryError):
            self.restore(node="reader-node")
        path = self.data / "bootstrap.yaml"
        original = path.read_bytes()
        value = yaml.safe_load(original)
        value["node_id"] = "other-node"
        path.write_text(yaml.safe_dump(value))
        with self.assertRaises(RecoveryIdentityConflict):
            self.restore()
        path.write_bytes(original)
        secret = next((self.data / "node-secrets").glob("*.json"))
        original_secret = secret.read_bytes()
        dataset = json.loads(original_secret)
        dataset["values"]["EXTRA"] = "disagreeing local value"
        secret.write_bytes(canonical_bytes(dataset))
        with self.assertRaises(RecoveryIdentityConflict):
            self.restore()
        secret.write_bytes(original_secret)
        self.assertEqual({str(path): path.read_bytes() for path in self.data.rglob("*") if path.is_file()}, before)

    def test_descriptor_change_during_restore_never_publishes_identity(self):
        self.provision()
        source = self.source()
        self.drive.descriptor_etags[source.file_id] += 1
        with self.assertRaises(RecoveryConflict):
            self.restore(source=source)
        self.assertFalse(self.data.exists())

    def test_full_format_box_simulation_without_old_private_files(self):
        self.provision()
        expected = self.secret_store.export_dataset()
        # Destroy machine A's entire fixture, including bootstrap/secrets/assets.
        shutil.rmtree(self.fixture.directory)
        self.assertFalse(self.data.exists())
        creds = Credentials(token="test-access", refresh_token="test-refresh-private", token_uri="https://oauth2.googleapis.com/token",
                            client_id="test-client", client_secret="client-private")
        client = self.box / "oauth-client.json"
        client.write_text(json.dumps(CLIENT))
        credential_bytes, _ = obtain_credentials(client_path=client, flow_factory=Mock(return_value=Mock(run_local_server=Mock(return_value=creds))))
        source = select_source(discover_sources(self.drive))
        result = restore_cloud_node(source, select_node(source), PASSPHRASE, credential_bytes, self.drive,
                                   data_dir=self.data, default_path=self.default_path, activate=True)
        self.assertEqual(result.provider, "google_drive")
        self.assertEqual(json.loads(next((self.data / "node-secrets").glob("*.json")).read_bytes()), expected)
        self.assertFalse((self.data / ".config.yaml").exists())
        self.assertFalse((self.data / "config.d").exists())
        self.assert_no_runtime_state()

    def test_final_credentials_and_secret_publication_permissions(self):
        self.provision()
        self.restore()
        for path in (self.data / "drive-credentials.json", self.data / "bootstrap.yaml",
                     next((self.data / "node-secrets").glob("*.json"))):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.data.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.data / "node-secrets").stat().st_mode & 0o777, 0o700)


class RecoveryOAuthCLITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def test_existing_credential_file_workflow_and_scope(self):
        path = self.directory / "credentials.json"
        path.write_bytes(CREDENTIALS)
        content, session = obtain_credentials(existing_path=path)
        self.assertEqual(content, CREDENTIALS)
        self.assertEqual(session.credentials.scopes, GoogleDriveTransport.SCOPES)
        self.assertEqual(path.read_bytes(), CREDENTIALS)

    def test_installed_oauth_pkce_loopback_narrow_scope_no_early_write(self):
        path = self.directory / "client.json"
        path.write_text(json.dumps(CLIENT))
        creds = Credentials(token="private-access", refresh_token="test-refresh-private", token_uri="https://oauth2.googleapis.com/token",
                            client_id="test-client", client_secret="client-private")
        flow = Mock(run_local_server=Mock(return_value=creds))
        factory = Mock(return_value=flow)
        content, _ = obtain_credentials(client_path=path, flow_factory=factory)
        self.assertEqual(json.loads(content)["refresh_token"], "test-refresh-private")
        self.assertEqual(factory.call_args.kwargs["scopes"], GoogleDriveTransport.SCOPES)
        self.assertTrue(factory.call_args.kwargs["autogenerate_code_verifier"])
        self.assertEqual(flow.run_local_server.call_args.kwargs["host"], "127.0.0.1")
        self.assertEqual(flow.run_local_server.call_args.kwargs["port"], 8765)
        self.assertEqual(flow.run_local_server.call_args.kwargs["timeout_seconds"], 600)
        self.assertEqual(set(item.name for item in self.directory.iterdir()), {"client.json"})

    def test_oauth_errors_and_library_token_logs_are_sanitized(self):
        path = self.directory / "client.json"
        path.write_text(json.dumps(CLIENT))

        def fail(*args, **kwargs):
            logging.getLogger("oauthlib").critical(PRIVATE)
            raise OSError(PRIVATE)

        output = io.StringIO()
        handler = logging.StreamHandler(output)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        with self.assertRaises(RecoveryError) as raised:
            obtain_credentials(client_path=path, flow_factory=fail)
        self.assertNotIn(PRIVATE, str(raised.exception))
        self.assertNotIn(PRIVATE, output.getvalue())
        self.assertTrue(raised.exception.__suppress_context__)

    def test_headless_login_shows_matching_tunnel_and_no_browser(self):
        path = self.directory / "client.json"
        path.write_text(json.dumps(CLIENT))
        creds = Credentials(token="private-access", refresh_token="test-refresh-private", token_uri="https://oauth2.googleapis.com/token",
                            client_id="test-client", client_secret="client-private")
        flow = Mock(run_local_server=Mock(return_value=creds))
        output = io.StringIO()
        with patch.dict("os.environ", {"SSH_CONNECTION": "present"}), redirect_stdout(output):
            self.assertFalse(browser_available())
            obtain_credentials(client_path=path, flow_factory=Mock(return_value=flow),
                               oauth_port=8877, oauth_timeout=900, ssh_target="robot@deskbox")
        self.assertFalse(flow.run_local_server.call_args.kwargs["open_browser"])
        self.assertEqual(flow.run_local_server.call_args.kwargs["port"], 8877)
        self.assertIn("127.0.0.1:8877:127.0.0.1:8877 robot@deskbox", output.getvalue())
        for private in ("private-access", "test-refresh-private", "client-private", str(path)):
            self.assertNotIn(private, output.getvalue())

    def test_oauth_client_validation_and_port_failure_are_safe(self):
        path = self.directory / "client.json"
        invalid = [{"web": CLIENT["installed"]}, {"installed": {}},
                   {"installed": {**CLIENT["installed"], "token_uri": PRIVATE}}]
        for value in invalid:
            path.write_text(json.dumps(value))
            with self.assertRaises(OAuthClientError) as raised:
                load_oauth_client(path)
            self.assertNotIn(PRIVATE, str(raised.exception))
        path.write_text(json.dumps(CLIENT))
        flow = Mock(run_local_server=Mock(side_effect=OSError(errno.EADDRINUSE, PRIVATE)))
        with redirect_stdout(io.StringIO()), self.assertRaises(OAuthPortError) as raised:
            obtain_credentials(client_path=path, flow_factory=Mock(return_value=flow), open_browser=False)
        self.assertNotIn(PRIVATE, str(raised.exception))
        self.assertTrue(raised.exception.__suppress_context__)

    def test_setup_private_files_are_create_only_and_symlinks_reject(self):
        path = self.directory / "data/oauth-client.json"
        content = canonical_bytes(CLIENT)
        install_oauth_file(path, content)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        install_oauth_file(path, content)
        with self.assertRaises(OAuthStorageError):
            install_oauth_file(path, canonical_bytes({"different": PRIVATE}))
        self.assertEqual(path.read_bytes(), content)
        link = path.parent / "symlink.json"
        link.symlink_to(path)
        with self.assertRaises(OAuthStorageError):
            install_oauth_file(link, content)

    def test_setup_import_authorize_and_reuse_without_cloud_or_bootstrap(self):
        cli = self.cli("setup_google_drive")
        client = self.directory / "download.json"
        client.write_text(json.dumps(CLIENT))
        loader = Mock(return_value=(CREDENTIALS, Mock()))
        output = io.StringIO()
        with patch.object(cli, "PROJECT", self.directory), redirect_stdout(output):
            self.assertEqual(cli.main(["--import-client", str(client), "--authorize", "--no-browser"], credential_loader=loader), 0)
            self.assertEqual(cli.main(["--authorize"], credential_loader=loader), 0)
        self.assertEqual(loader.call_count, 1)
        self.assertFalse(loader.call_args.kwargs["open_browser"])
        self.assertEqual(set(p.name for p in (self.directory / "data").iterdir()),
                         {"oauth-client.json", "drive-credentials.json"})
        self.assertEqual((self.directory / "data/drive-credentials.json").read_bytes(), CREDENTIALS)
        self.assertNotIn("test-refresh-private", output.getvalue())

    def test_restore_auto_client_and_oauth_options_forwarding(self):
        cli = self.cli("restore_cloud_node")
        (self.directory / "data").mkdir()
        (self.directory / "data/oauth-client.json").write_text(json.dumps(CLIENT))
        loader = Mock(side_effect=RecoveryError())
        with patch.object(cli, "PROJECT", self.directory), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["--no-browser", "--oauth-port", "8877", "--ssh-target", "robot@deskbox"], credential_loader=loader), 4)
        self.assertEqual(loader.call_args.kwargs["client_path"], self.directory / "data/oauth-client.json")
        self.assertEqual(loader.call_args.kwargs["oauth_port"], 8877)
        self.assertEqual(loader.call_args.kwargs["ssh_target"], "robot@deskbox")

    def test_passphrase_confirmation_and_no_echo_fallback(self):
        self.assertEqual(recovery_passphrase(confirm=True, prompt=Mock(side_effect=[PASSPHRASE, PASSPHRASE])), PASSPHRASE)
        with self.assertRaises(RecoveryError):
            recovery_passphrase(confirm=True, prompt=Mock(side_effect=[PASSPHRASE, "different"]))

        def fallback(message):
            import getpass
            warnings.warn("No hidden input available", getpass.GetPassWarning)
            self.fail("Unsafe fallback must not read passphrase")

        with self.assertRaises(RecoveryError):
            recovery_passphrase(prompt=fallback)

    def cli(self, name):
        path = Path(__file__).resolve().parents[3] / f"scripts/{name}.py"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_restore_cli_sanitizes_paths_tokens_passphrase_and_parse_errors(self):
        cli = self.cli("restore_cloud_node")
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            self.assertEqual(cli.main(["--credentials", PRIVATE], credential_loader=Mock(side_effect=OSError(PRIVATE))), 4)
            self.assertEqual(cli.main(["--passphrase", PASSPHRASE]), 4)
        for value in (PRIVATE, PASSPHRASE):
            self.assertNotIn(value, output.getvalue())

    def test_restore_cli_explicit_selection_and_provider_activation_forwarded_once(self):
        from config.cloud_restore import RestoreResult
        fixture = CloudRecoveryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.provision()
        source = fixture.source()
        cli = self.cli("restore_cloud_node")
        output = io.StringIO()
        restorer = Mock(return_value=RestoreResult(5, 1, "google_drive"))
        with redirect_stdout(output), redirect_stderr(output):
            result = cli.main(["--credentials", "safe-input.json", "--source-id", source.descriptor["source_id"],
                               "--node-id", "test-node", "--activate"],
                credential_loader=Mock(return_value=(CREDENTIALS, Mock())), transport_factory=Mock(return_value=fixture.drive),
                restorer=restorer, prompt=Mock(return_value=PASSPHRASE))
        self.assertEqual(result, 0)
        self.assertEqual(restorer.call_count, 1)
        self.assertEqual(restorer.call_args.args[1], "test-node")
        self.assertEqual(restorer.call_args.kwargs, {"activate": True})
        self.assertNotIn(PASSPHRASE, output.getvalue())
        self.assertNotIn("test-refresh-private", output.getvalue())

    def test_provisioning_cli_confirms_hidden_passphrase_before_recoverable_work(self):
        from config.cloud_provisioning import ProvisioningResult
        cli = self.cli("provision_cloud_state")
        output, prompt = io.StringIO(), Mock(side_effect=[PASSPHRASE, PASSPHRASE])
        with patch.object(cli, "load_bootstrap", return_value={"config_provider": "local", "node_id": "test-node"}), \
                patch.object(cli, "provision_cloud_state", return_value=ProvisioningResult(5, 1, False, "descriptor")) as provision, \
                redirect_stdout(output), redirect_stderr(output):
            self.assertEqual(cli.main(["--from-local"], prompt=prompt), 0)
        self.assertEqual(prompt.call_count, 2)
        self.assertEqual(provision.call_args.kwargs["recovery_passphrase"], PASSPHRASE)
        self.assertNotIn(PASSPHRASE, output.getvalue())
        self.assertIn("Provider: still local", output.getvalue())

    def test_drive_discovery_paginates_private_properties_no_scope_expansion(self):
        source_id = str(uuid.uuid4())
        items = [{"id": "descriptor", "properties": [
            {"key": "xiaozhi_cloud_state", "value": "v1", "visibility": "PRIVATE"},
            {"key": "source_id", "value": source_id, "visibility": "PRIVATE"}]}]
        session = Mock()
        session.request.side_effect = [Mock(status_code=200, json=Mock(return_value={"items": [], "nextPageToken": "second"})),
                                       Mock(status_code=200, json=Mock(return_value={"items": items}))]
        transport = GoogleDriveTransport("unused.json", session=session)
        self.assertEqual(transport.list_state_descriptors(), [{"file_id": "descriptor", "source_id": source_id}])
        self.assertEqual(session.request.call_args.kwargs["params"]["pageToken"], "second")
        self.assertIn("visibility='PRIVATE'", session.request.call_args.kwargs["params"]["q"])
        self.assertEqual(transport.SCOPES, ["https://www.googleapis.com/auth/drive.file"])
