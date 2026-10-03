"""Cloud desired/active configuration without credentials or network access."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from config.bootstrap import load_bootstrap, save_bootstrap
from config.cloud_layers import initial_cloud_object, resolve_layers, validate_layers
from config.cloud_secrets import LocalSecretStore
from config.config_store import (
    ConfigConflict, ConfigUnavailable, LocalConfigStoreAdapter, canonical_bytes,
    checksum, stage_provider_switch,
)
from config.config_validation import validate_config
from config.drive_transport import GoogleDriveTransport
from config.google_drive_config import GoogleDriveConfigStore, parse_manifest, validate_object
from core.utils.config_editor import ConfigEditor


class MemoryDrive:
    def __init__(self, defaults, overrides):
        self.files = {}
        self.manifest = None
        self.etag = 0
        self.offline = False
        self.manifest_read_race = False
        self.created_folders = []
        self.upload_folders = []
        self.fail_upload = False
        self.fail_commit = False
        self.corrupt_upload = False
        self.before_commit = None
        self.uploads = 0
        self.publish(defaults, overrides, 1)

    def publish(self, defaults, overrides, revision):
        self.publish_object({"schema_version": 1, "layers": {
            "defaults": defaults, "overrides": overrides,
        }}, revision)

    def publish_object(self, obj, revision):
        content = canonical_bytes(obj)
        file_id = f"object-{len(self.files)}"
        self.files[file_id] = content
        self.manifest = {"schema_version": 1, "revision": revision, "config": {
            "file_id": file_id, "sha256": checksum(content),
        }}
        self.etag += 1

    def read_manifest(self, file_id):
        if self.manifest_read_race:
            raise ConfigConflict("Simulated ETag change while reading manifest")
        if self.offline:
            raise ConfigUnavailable("Offline test transport")
        return canonical_bytes(self.manifest), str(self.etag)

    def download(self, file_id):
        if self.offline:
            raise ConfigUnavailable("Offline test transport")
        return self.files[file_id]

    def create_folder(self, name):
        self.created_folders.append(name)
        return "created-folder"

    def upload_immutable(self, folder_id, content, name):
        if self.fail_upload:
            raise ConfigUnavailable("Simulated upload failure")
        self.uploads += 1
        self.upload_folders.append(folder_id)
        file_id = f"upload-{self.uploads}"
        self.files[file_id] = b"corrupt" if self.corrupt_upload else content
        return file_id

    def upload_blob(self, folder_id, content, name, mime_type):
        return self.upload_immutable(folder_id, content, name)

    def replace_manifest(self, file_id, content, etag):
        if self.before_commit:
            callback, self.before_commit = self.before_commit, None
            callback()
        if etag != str(self.etag):
            raise ConfigConflict("Simulated conditional update conflict")
        if self.fail_commit:
            raise ConfigUnavailable("Simulated failure before publication")
        self.manifest = json.loads(content)
        self.etag += 1


class CloudConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.defaults = {
            "server": {"port": 8000, "http_port": 8003},
            "selected_module": {"LLM": "Test"},
            "LLM": {"Test": {"api_key": "your_key", "temperature": 0.7}},
            "log": {"log_level": "INFO"},
            "static_soundbank": {"directory": str(self.directory / "soundbank"), "entries": {}},
        }
        self.overrides = {"static_soundbank": {"entries": {"Hello": {"file": "hello.wav"}}},
                          "LLM": {"Test": {"api_key": "private-test-key"}},
                          "context_providers": [{"name": "Test", "headers": {
                              "Authorization": "private-test-header"}}]}
        self.default_path = self.directory / "config.yaml"
        (self.directory / "soundbank").mkdir()
        (self.directory / "soundbank/hello.wav").write_bytes(b"retained test sound")
        self.default_path.write_text(yaml.safe_dump(self.defaults))
        self.bootstrap = {"config_provider": "google_drive", "node_id": "test-node",
                          "google_drive": {"folder_id": "folder", "manifest_file_id": "manifest",
                                           "credentials_path": "private-credentials.json"}}
        self.secrets = LocalSecretStore("test-node", self.directory / "secrets")
        self.cloud_overrides, pending = self.secrets.externalize(self.overrides)
        self.secrets.put_many(pending)
        self.drive = MemoryDrive(self.defaults, self.cloud_overrides)
        self.store = self.new_cloud()

    def new_cloud(self):
        return GoogleDriveConfigStore(self.bootstrap, transport=self.drive,
                                      cache_dir=self.directory / "cache", validator=validate_config,
                                      default_path=str(self.default_path), secret_provider=self.secrets)

    def local(self):
        path = self.directory / "local/.config.yaml"
        path.parent.mkdir(exist_ok=True)
        path.write_text(yaml.safe_dump(self.overrides))
        return LocalConfigStoreAdapter({"config_provider": "local", "node_id": "test-node"},
                                       local_path=path, default_path=str(self.default_path))

    def boot(self, store=None):
        store = store or self.store
        store.prepare_runtime()
        store.mark_applied()
        return store

    def status(self, store=None):
        store = store or self.store
        with store.locked():
            return store.status_unlocked()

    def test_missing_bootstrap_defaults_to_local_without_creating_file(self):
        path = self.directory / "bootstrap.yaml"
        with patch("config.node_identity.socket.gethostname", return_value="deskbox"):
            self.assertEqual(load_bootstrap(path), {"config_provider": "local", "node_id": "deskbox"})
        self.assertFalse(path.exists())

    def test_bootstrap_rejects_missing_cloud_metadata(self):
        path = self.directory / "bootstrap.yaml"
        path.write_text("config_provider: google_drive\nnode_id: test-node\n")
        with self.assertRaises(ValueError):
            load_bootstrap(path)

    def test_local_adapter_read_and_save_preserve_sectioned_behavior(self):
        store = self.boot(self.local())
        editor = ConfigEditor(store)
        before = editor.read_public()
        result = editor.update({"server": {"port": 8001}})
        self.assertEqual(result["config"]["server"]["port"], 8001)
        self.assertTrue((store.local.sections_dir / "runtime.yaml").exists())
        self.assertEqual(store.local._read_object(store.local.directory / ".config.yaml.backup"), self.overrides)
        self.assertNotEqual(before["configuration_source"]["desired_revision"],
                            result["configuration_source"]["desired_revision"])

    def test_manifest_schema_validation(self):
        self.assertEqual(parse_manifest(canonical_bytes(self.drive.manifest)), self.drive.manifest)
        for invalid in ([], {}, {**self.drive.manifest, "schema_version": True},
                        {**self.drive.manifest, "revision": True},
                        {**self.drive.manifest, "revision": 0},
                        {**self.drive.manifest, "config": {"file_id": "bad/path", "sha256": "x"}}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_manifest(canonical_bytes(invalid))

    def test_checksum_mismatch_is_rejected_without_lkg(self):
        self.drive.files[self.drive.manifest["config"]["file_id"]] = b"tampered"
        with self.assertRaises(ConfigUnavailable):
            self.store.prepare_runtime()
        self.assertFalse((self.store.cache_dir / "desired.json").exists())

    def test_revision_conflict_does_not_upload_or_overwrite(self):
        self.boot()
        self.drive.publish(self.defaults, {"server": {"port": 8010}}, 2)
        with self.assertRaises(ConfigConflict):
            ConfigEditor(self.store).update({"server": {"port": 8001}}, base_revision=1)
        self.assertEqual(self.drive.manifest["revision"], 2)
        self.assertEqual(self.drive.uploads, 0)
        self.assertTrue(self.status()["conflict"])

    def test_cloud_save_requires_integer_base_revision(self):
        self.boot()
        for revision in (None, True, "1"):
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                ConfigEditor(self.store).update({"server": {"port": 8001}}, base_revision=revision)
        self.assertEqual(self.drive.uploads, 0)

    def test_upload_and_precommit_failures_keep_old_manifest_and_lkg(self):
        self.boot()
        for flag in ("fail_upload", "corrupt_upload", "fail_commit"):
            before = (self.store.cache_dir / "desired.json").read_bytes()
            manifest = copy.deepcopy(self.drive.manifest)
            setattr(self.drive, flag, True)
            with self.subTest(flag=flag), self.assertRaises(ConfigUnavailable):
                ConfigEditor(self.store).update({"server": {"port": 8001}}, base_revision=1)
            self.assertEqual(self.drive.manifest, manifest)
            self.assertEqual((self.store.cache_dir / "desired.json").read_bytes(), before)
            setattr(self.drive, flag, False)

    def test_manifest_cas_rejects_writer_that_raced_after_read(self):
        self.boot()
        self.drive.before_commit = lambda: self.drive.publish(self.defaults, {"server": {"port": 8012}}, 2)
        with self.assertRaises(ConfigConflict):
            ConfigEditor(self.store).update({"server": {"port": 8001}}, base_revision=1)
        self.assertEqual(self.drive.manifest["revision"], 2)
        authoritative = json.loads(self.drive.files[self.drive.manifest["config"]["file_id"]])
        self.assertEqual(authoritative["layers"]["overrides"]["server"]["port"], 8012)
        self.assertEqual(self.status()["desired_revision"], 2)

    def test_invalid_cloud_configuration_preserves_valid_lkg(self):
        self.boot()
        before = (self.store.cache_dir / "desired.json").read_bytes()
        for invalid in ({"server": {"port": 0}}, {"server": None}, {"log": []}, {"ASR": []}, {"TTS": {"Test": None}},
                        {"LLM": {"Test": {"api_key": "invalid-plaintext-secret"}}}):
            self.drive.publish(self.defaults, invalid, 2)
            restarted = self.new_cloud()
            effective = restarted.prepare_runtime()
            self.assertEqual(effective["server"]["port"], 8000)
            self.assertEqual((self.store.cache_dir / "desired.json").read_bytes(), before)
            self.assertEqual(self.status(restarted)["sync_status"], "offline_or_invalid")

    def test_offline_drive_with_valid_lkg_boots_without_credentials(self):
        self.boot()
        self.drive.offline = True
        restarted = self.new_cloud()
        effective = restarted.prepare_runtime()
        restarted.mark_applied()
        self.assertEqual(effective["LLM"]["Test"]["api_key"], "private-test-key")
        self.assertEqual(self.status(restarted)["active_revision"], 1)

    def test_offline_drive_without_lkg_fails_instead_of_using_local_config(self):
        self.drive.offline = True
        with self.assertRaises(ConfigUnavailable):
            self.store.prepare_runtime()

    def test_offline_restart_runs_active_rev1_after_unapplied_rev2_save(self):
        self.boot()
        ConfigEditor(self.store).update({"server": {"port": 8001}}, base_revision=1)
        active_bytes = (self.store.cache_dir / "active.json").read_bytes()
        self.drive.offline = True
        restarted = self.new_cloud()
        effective = restarted.prepare_runtime()
        self.assertEqual(effective["server"]["port"], 8000)
        source = self.status(restarted)
        self.assertEqual((source["desired_revision"], source["active_revision"], source["runtime_revision"]), (2, 1, 1))
        self.assertEqual(source["runtime_source"], "active_lkg")
        self.assertEqual(source["sync_state"], "out_of_sync")
        self.assertEqual((self.store.cache_dir / "active.json").read_bytes(), active_bytes)
        restarted.mark_applied()
        self.assertEqual(self.status(restarted)["active_revision"], 1)
        self.assertEqual(json.loads((self.store.cache_dir / "desired.json").read_bytes())["payload"]["manifest"]["revision"], 2)

    def test_offline_desired_only_cache_is_not_runtime_lkg(self):
        self.store.prepare_runtime()
        self.assertTrue((self.store.cache_dir / "desired.json").exists())
        self.assertFalse((self.store.cache_dir / "active.json").exists())
        self.drive.offline = True
        with self.assertRaises(ConfigUnavailable):
            self.new_cloud().prepare_runtime()

    def test_offline_corrupt_active_with_valid_desired_fails_safely(self):
        self.boot()
        ConfigEditor(self.store).update({"server": {"port": 8001}}, base_revision=1)
        (self.store.cache_dir / "active.json").write_bytes(b"invalid active snapshot")
        self.drive.offline = True
        with self.assertRaises(ConfigUnavailable):
            self.new_cloud().prepare_runtime()

    def test_manifest_read_conflict_propagates_from_strict_refresh(self):
        self.boot()
        self.drive.manifest_read_race = True
        with self.store.locked(), self.assertRaises(ConfigConflict):
            self.store.refresh_unlocked(strict=True)
        self.assertTrue(self.status()["conflict"])
        self.assertEqual(self.status()["desired_revision"], 1)
        self.assertEqual(self.drive.uploads, 0)

    def central_object(self):
        obj = initial_cloud_object(self.cloud_overrides, "test-node")
        layers = obj["layers"]
        layers["global"] = {"server": {"port": 8005}}
        layers["environments"] = {"dev": {"server": {"port": 8006}},
                                  "prod": {"server": {"port": 8009}}}
        layers["roles"] = {"frontend": {"server": {"port": 8007}},
                           "worker": {"server": {"port": 8010}}}
        layers["nodes"]["test-node"].update(environment="dev", role="frontend")
        layers["nodes"]["other-node"] = {"environment": "prod", "role": "worker",
                                         "overrides": {"server": {"port": 9001}}}
        return obj

    def test_central_layers_resolve_in_order_and_keep_assignments_in_cloud(self):
        obj = self.central_object()
        obj["layers"]["nodes"]["test-node"]["overrides"]["server"] = {"port": 8008}
        self.drive.publish_object(obj, 1)
        self.assertEqual(self.store.prepare_runtime()["server"]["port"], 8008)
        self.store.mark_applied()
        source = self.status()
        self.assertEqual((source["environment"], source["role"]), ("dev", "frontend"))
        self.assertEqual(self.store.bootstrap, self.bootstrap)

    def test_settings_edit_preserves_shared_layers_and_other_nodes(self):
        obj = self.central_object()
        self.drive.publish_object(obj, 1)
        self.boot()
        result = ConfigEditor(self.store).update({"prompt": "Edited node prompt"}, base_revision=1)
        committed = json.loads(self.drive.files[self.drive.manifest["config"]["file_id"]])
        for scope in ("global", "environments", "roles"):
            self.assertEqual(committed["layers"][scope], obj["layers"][scope])
        self.assertEqual(committed["layers"]["nodes"]["other-node"], obj["layers"]["nodes"]["other-node"])
        node_overrides = committed["layers"]["nodes"]["test-node"]["overrides"]
        self.assertEqual(node_overrides["prompt"], "Edited node prompt")
        self.assertNotIn("server", node_overrides)
        self.assertEqual(result["config"]["server"]["port"], 8007)
        # An inherited role change continues to propagate after a node Settings save.
        committed["layers"]["roles"]["frontend"]["server"]["port"] = 8012
        self.drive.publish_object(committed, 3)
        with self.store.locked():
            self.store.refresh_unlocked(strict=True)
        self.assertEqual(ConfigEditor(self.store).read_public()["config"]["server"]["port"], 8012)

    def test_offline_runtime_keeps_applied_cloud_environment_and_role(self):
        obj = self.central_object()
        self.drive.publish_object(obj, 1)
        self.boot()
        obj["layers"]["nodes"]["test-node"].update(environment="prod", role="worker")
        self.drive.publish_object(obj, 2)
        with self.store.locked():
            self.store.refresh_unlocked(strict=True)
        self.drive.offline = True
        restarted = self.new_cloud()
        self.assertEqual(restarted.prepare_runtime()["server"]["port"], 8007)
        source = self.status(restarted)
        self.assertEqual((source["environment"], source["role"]), ("prod", "worker"))
        self.assertEqual((source["active_environment"], source["active_role"]), ("dev", "frontend"))

    def test_cloud_topology_validates_every_node_and_assignment(self):
        for mutate in (
            lambda layers: layers["nodes"]["other-node"].update(role="missing"),
            lambda layers: layers["nodes"]["other-node"]["overrides"].update(server={"port": 0}),
            lambda layers: layers["global"].update(node_id="cloud-node"),
        ):
            obj = self.central_object()
            mutate(obj["layers"])
            content = canonical_bytes(obj)
            manifest = {**self.drive.manifest, "config": {"file_id": "test", "sha256": checksum(content)}}
            with self.assertRaises(ValueError):
                validate_object(content, manifest, validate_config)

    def test_unassigned_local_node_is_rejected(self):
        obj = self.central_object()
        obj["layers"]["nodes"].pop("test-node")
        self.drive.publish_object(obj, 1)
        with self.assertRaises(ConfigUnavailable):
            self.store.prepare_runtime()

    def test_central_active_cache_cannot_be_used_by_different_local_identity(self):
        self.drive.publish_object(self.central_object(), 1)
        self.boot()
        self.drive.offline = True
        other = GoogleDriveConfigStore({**self.bootstrap, "node_id": "other-node"},
                                       transport=self.drive, cache_dir=self.store.cache_dir)
        with self.assertRaises(ConfigUnavailable):
            other.prepare_runtime()

    def test_legacy_layout_save_remains_legacy_without_implicit_migration(self):
        self.boot()
        ConfigEditor(self.store).update({"server": {"port": 8001}}, base_revision=1)
        obj = json.loads(self.drive.files[self.drive.manifest["config"]["file_id"]])
        self.assertEqual(set(obj["layers"]), {"defaults", "overrides"})

    def test_desired_revision_differs_from_active_after_save(self):
        self.boot()
        result = ConfigEditor(self.store).update({"server": {"port": 8001}}, base_revision=1)
        source = result["configuration_source"]
        self.assertEqual((source["desired_revision"], source["active_revision"]), (2, 1))
        self.assertEqual(source["sync_state"], "out_of_sync")
        self.assertTrue(result["restart_required"])

    def test_successful_restart_advances_active_revision(self):
        self.boot()
        ConfigEditor(self.store).update({"server": {"port": 8001}}, base_revision=1)
        restarted = self.new_cloud()
        self.assertEqual(restarted.prepare_runtime()["server"]["port"], 8001)
        self.assertEqual(self.status(restarted)["active_revision"], 1)
        restarted.mark_applied()
        self.assertEqual(self.status(restarted)["active_revision"], 2)
        self.assertEqual(self.status(restarted)["sync_state"], "in_sync")

    def test_apply_marks_boot_snapshot_even_if_new_desired_is_saved(self):
        self.store.prepare_runtime()
        ConfigEditor(self.store).update({"server": {"port": 8001}}, base_revision=1)
        self.store.mark_applied()
        self.assertEqual(self.status()["active_revision"], 1)
        self.assertEqual(self.status()["desired_revision"], 2)

    def test_public_response_masks_secrets_and_blank_secret_save_preserves_values(self):
        for store in (self.local(), self.store):
            self.boot(store)
            editor = ConfigEditor(store)
            result = editor.update({"LLM": {"Test": {"api_key": ""}},
                                    "context_providers": [{"name": "Test", "headers": {"Authorization": ""}}]},
                                   base_revision=1)
            public = json.dumps(result)
            self.assertNotIn("private-test-key", public)
            self.assertNotIn("private-test-header", public)
            self.assertNotIn("private-credentials.json", public)
            self.assertIn("LLM.Test.api_key", result["configured_secrets"])
            with store.locked():
                desired = store.read_unlocked()
                if store is self.store:
                    desired = self.secrets.resolve(desired)
            self.assertEqual(desired["LLM"]["Test"]["api_key"], "private-test-key")
            self.assertEqual(desired["context_providers"][0]["headers"]["Authorization"], "private-test-header")

    def test_provider_and_source_metadata(self):
        for store in (self.local(), self.store):
            self.boot(store)
            source = ConfigEditor(store).read_public()["configuration_source"]
            self.assertEqual(source["node_id"], "test-node")
            self.assertEqual(source["config_provider"], store.bootstrap["config_provider"])
            self.assertIn("source", source)
            self.assertIn("active_source", source)
            self.assertIn("cache_path", source)
            self.assertIn("last_sync", source)
            self.assertIn("last_error", source)

    def test_validation_and_soundbank_map_replacement_are_shared(self):
        for store in (self.local(), self.store):
            self.boot(store)
            editor = ConfigEditor(store)
            with self.assertRaises(ValueError):
                editor.update({"server": {"port": 0}}, base_revision=1)
            result = editor.update({"static_soundbank": {"entries": {}}}, base_revision=1)
            self.assertEqual(result["config"]["static_soundbank"]["entries"], {})

    def test_invalid_cloud_does_not_overwrite_bootstrap_identity(self):
        self.drive.publish(self.defaults, {"node_id": "cloud-node"}, 2)
        with self.assertRaises(ConfigUnavailable):
            self.store.prepare_runtime()
        self.assertEqual(self.store.bootstrap["node_id"], "test-node")

    def test_corrupt_desired_cache_falls_back_to_active_snapshot(self):
        self.boot()
        (self.store.cache_dir / "desired.json").write_bytes(b"broken")
        self.drive.offline = True
        restarted = self.new_cloud()
        self.assertEqual(restarted.prepare_runtime()["server"]["port"], 8000)

    def test_cached_revision_and_checksum_cannot_be_changed_independently(self):
        self.boot()
        path = self.store.cache_dir / "desired.json"
        content = json.loads(path.read_bytes())
        content["payload"]["manifest"]["revision"] = 99
        path.write_bytes(canonical_bytes(content))
        (self.store.cache_dir / "active.json").unlink()
        self.drive.offline = True
        with self.assertRaises(ConfigUnavailable):
            self.new_cloud().prepare_runtime()

    def test_cloud_cannot_reuse_revision_or_roll_back(self):
        self.boot()
        for revision in (1,):
            self.drive.publish(self.defaults, {"server": {"port": 8010}}, revision)
            with self.store.locked(), self.assertRaises(ConfigUnavailable):
                self.store.refresh_unlocked(strict=True)
        self.assertEqual(self.status()["desired_revision"], 1)

    def test_cloud_commit_success_is_reported_if_cache_write_fails(self):
        self.boot()
        with patch.object(self.store, "_write_cache", side_effect=OSError("Disk full")):
            result = ConfigEditor(self.store).update({"server": {"port": 8001}}, base_revision=1)
        self.assertEqual(self.drive.manifest["revision"], 2)
        self.assertEqual(result["configuration_source"]["desired_revision"], 2)
        self.assertEqual(result["configuration_source"]["sync_status"], "cache_error")

    def test_cache_permissions_are_private(self):
        self.boot()
        for name in ("desired.json", "active.json"):
            self.assertEqual((self.store.cache_dir / name).stat().st_mode & 0o777, 0o600)

    def test_explicit_source_switch_does_not_merge_or_switch_running_store(self):
        local = self.boot(self.local())
        original_local = local.local.local_path.read_bytes()
        with patch("config.config_store.get_config_store", return_value=local), \
             patch("config.config_store.load_bootstrap", return_value={**self.bootstrap, "config_provider": "local"}), \
             patch("config.config_store.create_config_store", return_value=self.store), \
             patch("config.config_store.save_bootstrap", side_effect=lambda value: save_bootstrap(
                 value, local.local.directory / "bootstrap.yaml")) as save:
            source = stage_provider_switch("google_drive")
            self.assertEqual(save.call_args.args[0]["node_id"], "test-node")
            self.assertEqual(save.call_args.args[0]["config_provider"], "google_drive")
        self.assertEqual(source["config_provider"], "local")
        self.assertEqual(source["pending_provider"], "google_drive")
        self.assertEqual(load_bootstrap(local.local.directory / "bootstrap.yaml")["config_provider"], "google_drive")
        self.assertEqual(local.local.local_path.read_bytes(), original_local)
        with self.assertRaises(ValueError):
            ConfigEditor(local).update({"server": {"port": 8001}})

    def test_cloud_source_switch_readiness_validates_resolved_runtime_without_apply(self):
        obj = self.central_object()
        self.drive.publish_object(obj, 1)
        local = self.boot(self.local())
        bootstrap_path = local.local.directory / "bootstrap.yaml"
        local_bootstrap = {**self.bootstrap, "config_provider": "local"}
        save_bootstrap(local_bootstrap, bootstrap_path)
        original_manifest = copy.deepcopy(self.drive.manifest)
        original_files = copy.deepcopy(self.drive.files)
        with patch("config.config_store.get_config_store", return_value=local), \
             patch("config.config_store.load_bootstrap", side_effect=lambda: load_bootstrap(bootstrap_path)), \
             patch("config.config_store.create_config_store", return_value=self.store), \
             patch("config.config_store.save_bootstrap", side_effect=lambda value: save_bootstrap(value, bootstrap_path)):
            source = stage_provider_switch("google_drive")
        self.assertEqual(load_bootstrap(bootstrap_path)["config_provider"], "google_drive")
        self.assertEqual(local.bootstrap["config_provider"], "local")
        self.assertEqual(source["pending_provider"], "google_drive")
        self.assertIsNone(self.store.runtime_snapshot)
        self.assertIsNone(self.store.runtime_source)
        self.assertIsNone(self.store.active_snapshot)
        self.assertIsNone(self.store.active_revision)
        self.assertFalse((self.store.cache_dir / "active.json").exists())
        self.assertEqual(self.drive.manifest, original_manifest)
        self.assertEqual(self.drive.files, original_files)
        self.assertEqual(self.drive.uploads, 0)

    def test_missing_inherited_secret_rejects_switch_and_preserves_local_bootstrap(self):
        obj = self.central_object()
        missing_name = "MISSING_NODE_GEMINI_KEY"
        # A required secret inherited from a role must also pass readiness.
        obj["layers"]["nodes"]["test-node"]["overrides"]["LLM"]["Test"].pop("api_key")
        obj["layers"]["roles"]["frontend"]["LLM"] = {"Test": {"api_key": "${secret:" + missing_name + "}"}}
        self.drive.publish_object(obj, 1)
        local = self.boot(self.local())
        local_snapshot = copy.deepcopy(local.runtime_snapshot)
        local_revision = local.active_revision
        bootstrap_path = local.local.directory / "bootstrap.yaml"
        save_bootstrap({**self.bootstrap, "config_provider": "local"}, bootstrap_path)
        original_bootstrap = bootstrap_path.read_bytes()
        original_manifest = copy.deepcopy(self.drive.manifest)
        original_files = copy.deepcopy(self.drive.files)
        original_secrets = self.secrets.path.read_bytes()
        with patch("config.config_store.get_config_store", return_value=local), \
             patch("config.config_store.load_bootstrap", side_effect=lambda: load_bootstrap(bootstrap_path)), \
             patch("config.config_store.create_config_store", return_value=self.store), \
             patch("config.config_store.save_bootstrap", side_effect=lambda value: save_bootstrap(value, bootstrap_path)) as save:
            with self.assertRaises(ConfigUnavailable) as raised:
                stage_provider_switch("google_drive")
            save.assert_not_called()
        self.assertEqual(bootstrap_path.read_bytes(), original_bootstrap)
        self.assertEqual(load_bootstrap(bootstrap_path)["config_provider"], "local")
        self.assertEqual(local.bootstrap["config_provider"], "local")
        self.assertIsNone(local.pending_provider)
        self.assertEqual(local.runtime_snapshot, local_snapshot)
        self.assertEqual(local.active_revision, local_revision)
        self.assertIsNone(self.store.runtime_snapshot)
        self.assertFalse((self.store.cache_dir / "active.json").exists())
        self.assertEqual(self.drive.manifest, original_manifest)
        self.assertEqual(self.drive.files, original_files)
        self.assertEqual(self.secrets.path.read_bytes(), original_secrets)
        self.assertEqual(self.drive.uploads, 0)
        for private in (missing_name, "private-test-key", "private-test-header"):
            self.assertNotIn(private, str(raised.exception))
            self.assertNotIn(private, repr(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)

    def test_readiness_validates_final_config_after_secret_resolution_and_redacts_errors(self):
        self.drive.publish_object(self.central_object(), 1)
        seen = []

        def validator(config):
            validate_config(config)
            if config["LLM"]["Test"]["api_key"] == "private-test-key":
                seen.append(config["server"]["port"])
                raise ValueError("Invalid runtime private-test-key")

        self.store.validator = validator
        with self.assertRaises(ConfigUnavailable) as raised:
            self.store.validate_runtime_readiness()
        self.assertEqual(seen, [8007])
        self.assertNotIn("private-test-key", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(self.store.runtime_snapshot)
        self.assertFalse((self.store.cache_dir / "active.json").exists())
        self.assertEqual(self.drive.uploads, 0)

    def test_readiness_does_not_advance_existing_active_or_boot_snapshot(self):
        obj = self.central_object()
        self.drive.publish_object(obj, 1)
        self.boot()
        active_path = self.store.cache_dir / "active.json"
        original_cache = active_path.read_bytes()
        original_runtime = copy.deepcopy(self.store.runtime_snapshot)
        original_active = copy.deepcopy(self.store.active_snapshot)
        self.defaults["server"]["http_port"] = 8014
        self.default_path.write_text(yaml.safe_dump(self.defaults))
        obj["layers"]["nodes"]["test-node"]["overrides"]["prompt"] = "Preflight desired edit"
        self.drive.publish_object(obj, 2)
        original_manifest = copy.deepcopy(self.drive.manifest)
        self.store.validate_runtime_readiness()
        self.assertEqual(self.store.runtime_snapshot, original_runtime)
        self.assertEqual(self.store.active_snapshot, original_active)
        self.assertEqual(self.store.active_revision, 1)
        self.assertEqual(active_path.read_bytes(), original_cache)
        self.assertEqual(self.drive.manifest, original_manifest)
        self.assertEqual(self.drive.uploads, 0)
        self.assertEqual(self.status()["desired_revision"], 2)

    def test_readiness_requires_live_drive_even_with_valid_active_lkg(self):
        self.boot()
        self.drive.offline = True
        with self.assertRaises(ConfigUnavailable):
            self.store.validate_runtime_readiness()

    def test_readiness_preserves_manifest_read_conflicts(self):
        self.drive.manifest_read_race = True
        with self.assertRaises(ConfigConflict):
            self.store.validate_runtime_readiness()
        self.assertIsNone(self.store.runtime_snapshot)
        self.assertFalse((self.store.cache_dir / "active.json").exists())
        self.assertEqual(self.drive.uploads, 0)

    def test_local_revision_supports_existing_safe_yaml_values(self):
        store = self.local()
        with store.locked():
            values = store.read_unlocked()
            from datetime import date
            values["future_setting"] = {"date": date(2026, 1, 1)}
            store.local.write_unlocked(values)
        self.boot(store)
        result = ConfigEditor(store).update({"server": {"port": 8001}})
        self.assertEqual(result["config"]["server"]["port"], 8001)

    def test_apply_cache_failure_keeps_successful_runtime_active(self):
        self.store.prepare_runtime()
        with patch.object(self.store, "_write_cache", side_effect=OSError("Disk full")):
            self.store.mark_applied()
        self.assertEqual(self.status()["active_revision"], 1)
        self.assertEqual(self.status()["sync_status"], "cache_error")

    def test_switch_to_local_uses_retained_local_config_without_copying_cloud(self):
        local = self.local()
        self.boot()
        self.drive.publish(self.defaults, {"server": {"port": 8012}}, 2)
        with patch("config.config_store.get_config_store", return_value=self.store),              patch("config.config_store.load_bootstrap", return_value=self.bootstrap),              patch("config.config_store.create_config_store", return_value=local),              patch("config.config_store.save_bootstrap") as save:
            source = stage_provider_switch("local")
            self.assertEqual(save.call_args.args[0]["config_provider"], "local")
        self.assertEqual(source["config_provider"], "google_drive")
        self.assertEqual(source["pending_provider"], "local")
        self.assertEqual(local.prepare_runtime()["server"]["port"], 8000)

    def test_switch_to_local_rejects_missing_local_override(self):
        local = self.local()
        local.local.local_path.unlink()
        self.boot()
        with patch("config.config_store.get_config_store", return_value=self.store),              patch("config.config_store.load_bootstrap", return_value=self.bootstrap),              patch("config.config_store.create_config_store", return_value=local),              patch("config.config_store.save_bootstrap") as save:
            with self.assertRaises(ValueError):
                stage_provider_switch("local")
            save.assert_not_called()

    def test_source_switch_requires_live_valid_cloud(self):
        local = self.boot(self.local())
        self.drive.offline = True
        with patch("config.config_store.get_config_store", return_value=local), \
             patch("config.config_store.load_bootstrap", return_value=self.bootstrap), \
             patch("config.config_store.create_config_store", return_value=self.store), \
             patch("config.config_store.save_bootstrap") as save:
            with self.assertRaises(ConfigUnavailable):
                stage_provider_switch("google_drive")
            save.assert_not_called()


    def test_override_only_object_has_no_repo_defaults(self):
        obj = initial_cloud_object({}, "test-node")
        self.assertEqual(obj["layers"]["global"], {})
        self.assertNotIn("defaults", json.dumps(obj))
        baseline, node = resolve_layers(obj, "test-node", self.defaults)
        self.assertEqual(baseline, self.defaults)
        self.assertEqual(node, {})

    def test_repo_default_upgrade_requires_no_cloud_rewrite(self):
        obj = initial_cloud_object({}, "test-node")
        self.drive.publish_object(obj, 1)
        self.boot()
        original = copy.deepcopy(self.drive.manifest)
        self.defaults["wakeup_greeting"] = "New release greeting"
        self.default_path.write_text(yaml.safe_dump(self.defaults))
        restarted = self.new_cloud()
        self.assertEqual(restarted.prepare_runtime()["wakeup_greeting"], "New release greeting")
        self.assertEqual(self.drive.manifest, original)
        self.assertEqual(self.drive.uploads, 0)
        self.assertNotIn("New release greeting", (restarted.cache_dir / "desired.json").read_text())

    def test_offline_lkg_rejects_changed_release_defaults(self):
        self.drive.publish_object(initial_cloud_object(self.cloud_overrides, "test-node"), 1)
        self.boot()
        active_path = self.store.cache_dir / "active.json"
        applied = active_path.read_bytes()
        self.defaults["server"]["http_port"] = 8014
        self.default_path.write_text(yaml.safe_dump(self.defaults))
        self.drive.offline = True
        restarted = self.new_cloud()
        with self.assertRaises(ConfigUnavailable) as raised:
            restarted.prepare_runtime()
        self.assertIn("incompatible with current repo defaults", str(raised.exception))
        self.assertIsNone(restarted.runtime_snapshot)
        self.assertIsNone(restarted.runtime_source)
        self.assertEqual(active_path.read_bytes(), applied)

    def test_online_upgrade_applies_new_fingerprint_then_offline_boots(self):
        self.drive.publish_object(initial_cloud_object(self.cloud_overrides, "test-node"), 1)
        self.boot()
        active_path = self.store.cache_dir / "active.json"
        applied_a = active_path.read_bytes()
        original_manifest = copy.deepcopy(self.drive.manifest)
        self.defaults["server"]["http_port"] = 8014
        self.defaults["wakeup_greeting"] = "Release B greeting"
        self.default_path.write_text(yaml.safe_dump(self.defaults))
        restarted = self.new_cloud()
        runtime = restarted.prepare_runtime()
        self.assertEqual(runtime["server"]["http_port"], 8014)
        self.assertEqual(runtime["wakeup_greeting"], "Release B greeting")
        self.assertEqual(active_path.read_bytes(), applied_a)
        restarted.mark_applied()
        active_b = json.loads(active_path.read_bytes())
        self.assertEqual(active_b["payload"]["repo_defaults_sha256"], checksum(canonical_bytes(self.defaults)))
        self.assertNotIn("repo_defaults_sha256", json.loads((self.store.cache_dir / "desired.json").read_bytes())["payload"])
        self.assertEqual(self.drive.manifest, original_manifest)
        self.assertEqual(self.drive.uploads, 0)
        self.drive.offline = True
        offline = self.new_cloud()
        self.assertEqual(offline.prepare_runtime()["server"]["http_port"], 8014)
        self.assertEqual(self.status(offline)["runtime_source"], "active_lkg")
        self.assertNotIn("private-test-key", active_path.read_text())

    def test_defaults_fingerprint_uses_parsed_values_not_yaml_formatting(self):
        self.drive.publish_object(initial_cloud_object(self.cloud_overrides, "test-node"), 1)
        self.boot()
        active = json.loads((self.store.cache_dir / "active.json").read_bytes())
        self.assertEqual(active["payload"]["repo_defaults_sha256"], checksum(canonical_bytes(self.defaults)))
        reordered = dict(reversed(list(self.defaults.items())))
        self.default_path.write_text("# Same parsed release defaults\n" + yaml.safe_dump(reordered, sort_keys=False))
        self.drive.offline = True
        self.assertEqual(self.new_cloud().prepare_runtime()["server"]["http_port"], 8003)

    def test_apply_records_boot_defaults_without_reloading_changed_file(self):
        self.drive.publish_object(initial_cloud_object(self.cloud_overrides, "test-node"), 1)
        defaults_a = copy.deepcopy(self.defaults)
        construct_runtime = self.store._runtime_config

        def change_defaults_before_resolution(snapshot, captured_defaults):
            self.defaults["server"]["http_port"] = 8014
            self.default_path.write_text(yaml.safe_dump(self.defaults))
            return construct_runtime(snapshot, captured_defaults)

        with patch.object(self.store, "_runtime_config", side_effect=change_defaults_before_resolution):
            runtime = self.store.prepare_runtime()
        self.assertEqual(runtime["server"]["http_port"], 8003)
        self.assertFalse((self.store.cache_dir / "active.json").exists())
        self.store.mark_applied()
        applied = json.loads((self.store.cache_dir / "active.json").read_bytes())
        self.assertEqual(applied["payload"]["repo_defaults_sha256"], checksum(canonical_bytes(defaults_a)))
        self.drive.offline = True
        with self.assertRaises(ConfigUnavailable):
            self.new_cloud().prepare_runtime()

    def test_active_cache_without_defaults_fingerprint_requires_online_apply(self):
        for revision, obj in enumerate((initial_cloud_object(self.cloud_overrides, "test-node"),
                {"schema_version": 1, "layers": {"defaults": self.defaults, "overrides": self.cloud_overrides}}), start=1):
            self.drive.offline = False
            self.drive.publish_object(obj, revision)
            self.boot(self.new_cloud())
            active_path = self.store.cache_dir / "active.json"
            legacy = json.loads(active_path.read_bytes())
            legacy["payload"].pop("repo_defaults_sha256")
            legacy["sha256"] = checksum(canonical_bytes(legacy["payload"]))
            active_path.write_bytes(canonical_bytes(legacy))
            self.drive.offline = True
            with self.assertRaises(ConfigUnavailable):
                self.new_cloud().prepare_runtime()
            self.assertEqual(json.loads(active_path.read_bytes()), legacy)
            self.drive.offline = False
            restarted = self.new_cloud()
            restarted.prepare_runtime()
            self.assertEqual(json.loads(active_path.read_bytes()), legacy)
            restarted.mark_applied()
            self.drive.offline = True
            self.assertEqual(self.new_cloud().prepare_runtime()["server"]["port"], 8000)

    def test_active_defaults_fingerprint_is_bound_to_cache_checksum(self):
        self.drive.publish_object(initial_cloud_object(self.cloud_overrides, "test-node"), 1)
        self.boot()
        path = self.store.cache_dir / "active.json"
        active = json.loads(path.read_bytes())
        active["payload"]["repo_defaults_sha256"] = "0" * 64
        path.write_bytes(canonical_bytes(active))
        self.drive.offline = True
        with self.assertRaises(ConfigUnavailable):
            self.new_cloud().prepare_runtime()

    def test_resolution_order_includes_current_repo_baseline(self):
        obj = self.central_object()
        layers = obj["layers"]
        baseline, overrides = resolve_layers(obj, "test-node", self.defaults)
        self.assertEqual(baseline["server"]["port"], 8007)
        self.assertEqual(baseline["server"]["http_port"], 8003)
        layers["nodes"]["test-node"]["overrides"]["server"] = {"port": 8008}
        self.drive.publish_object(obj, 1)
        self.assertEqual(self.store.prepare_runtime()["server"]["port"], 8008)
        layers["nodes"]["test-node"]["role"] = None
        self.assertEqual(resolve_layers(obj, "test-node", self.defaults)[0]["server"]["port"], 8006)
        layers["nodes"]["test-node"]["environment"] = None
        self.assertEqual(resolve_layers(obj, "test-node", self.defaults)[0]["server"]["port"], 8005)
        layers["global"] = {}
        self.assertEqual(resolve_layers(obj, "test-node", self.defaults)[0]["server"]["port"], 8000)

    def test_validation_uses_current_defaults_for_every_node(self):
        obj = self.central_object()
        calls = []
        validate_layers(obj, lambda config: calls.append(config), self.defaults)
        self.assertEqual([config["server"]["port"] for config in calls], [8007, 9001])
        self.assertTrue(all(config["server"]["http_port"] == 8003 for config in calls))
        obj["layers"]["nodes"]["other-node"]["overrides"]["server"]["http_port"] = 0
        with self.assertRaises(ValueError):
            validate_layers(obj, validate_config, self.defaults)

    def test_cloud_cache_and_runtime_snapshot_never_store_resolved_secrets(self):
        self.assertEqual(self.store.prepare_runtime()["LLM"]["Test"]["api_key"], "private-test-key")
        self.store.mark_applied()
        for name in ("desired.json", "active.json"):
            content = (self.store.cache_dir / name).read_text()
            self.assertIn("${secret:", content)
            self.assertNotIn("private-test-key", content)
            self.assertNotIn("private-test-header", content)
        self.assertNotIn("private-test-key", json.dumps(self.store.runtime_snapshot))

    def test_node_cannot_resolve_another_nodes_secret_from_shared_object(self):
        obj = self.central_object()
        node_b = obj["layers"]["nodes"]["other-node"]
        node_b["overrides"]["LLM"] = {"Test": {"api_key": "${secret:WORKER_KEY}"}}
        other_secrets = LocalSecretStore("other-node", self.directory / "secrets")
        other_secrets.put_many({"WORKER_KEY": "worker-private-key"})
        self.drive.publish_object(obj, 1)
        self.boot()
        with self.assertRaises(ValueError):
            self.secrets.resolve(node_b["overrides"])
        other = GoogleDriveConfigStore({**self.bootstrap, "node_id": "other-node"},
            transport=self.drive, cache_dir=self.directory / "other-cache",
            default_path=str(self.default_path), secret_provider=other_secrets)
        self.assertEqual(other.prepare_runtime()["LLM"]["Test"]["api_key"], "worker-private-key")
        other.mark_applied()
        with self.assertRaises(ValueError):
            other_secrets.resolve(obj["layers"]["nodes"]["test-node"]["overrides"])
        for store in (self.store, other):
            for name in ("desired.json", "active.json"):
                content = (store.cache_dir / name).read_text()
                self.assertNotIn("worker-private-key", content)
                self.assertNotIn("private-test-key", content)

    def test_settings_blank_secret_keeps_reference_and_local_value(self):
        self.drive.publish_object(self.central_object(), 1)
        self.boot()
        original = self.secrets.path.read_bytes()
        reference = self.cloud_overrides["LLM"]["Test"]["api_key"]
        result = ConfigEditor(self.store).update({"LLM": {"Test": {"api_key": ""}}}, base_revision=1)
        obj = json.loads(self.drive.files[self.drive.manifest["config"]["file_id"]])
        self.assertEqual(obj["layers"]["nodes"]["test-node"]["overrides"]["LLM"]["Test"]["api_key"], reference)
        self.assertEqual(self.secrets.path.read_bytes(), original)
        self.assertEqual(result["config"]["LLM"]["Test"]["api_key"], "")

    def test_settings_secret_replacement_keeps_active_value_until_apply(self):
        self.drive.publish_object(self.central_object(), 1)
        self.boot()
        result = ConfigEditor(self.store).update({"LLM": {"Test": {"api_key": "replacement-private-key"}}}, base_revision=1)
        self.assertNotIn("replacement-private-key", json.dumps(result))
        for content in self.drive.files.values():
            self.assertNotIn(b"replacement-private-key", content)
        with self.store.locked():
            desired = self.store.read_unlocked()
        self.assertEqual(self.secrets.resolve(desired)["LLM"]["Test"]["api_key"], "replacement-private-key")
        for name in ("desired.json", "active.json"):
            self.assertNotIn("replacement-private-key", (self.store.cache_dir / name).read_text())
        self.drive.offline = True
        self.assertEqual(self.new_cloud().prepare_runtime()["LLM"]["Test"]["api_key"], "private-test-key")
        self.drive.offline = False
        restarted = self.new_cloud()
        self.assertEqual(restarted.prepare_runtime()["LLM"]["Test"]["api_key"], "replacement-private-key")
        restarted.mark_applied()
        self.assertEqual(self.status(restarted)["active_revision"], 2)

    def test_failed_secret_cas_does_not_change_active_secret(self):
        self.boot()
        self.drive.before_commit = lambda: self.drive.publish(self.defaults, self.cloud_overrides, 2)
        with self.assertRaises(ConfigConflict):
            ConfigEditor(self.store).update({"LLM": {"Test": {"api_key": "rejected-private-key"}}}, base_revision=1)
        self.drive.offline = True
        self.assertEqual(self.new_cloud().prepare_runtime()["LLM"]["Test"]["api_key"], "private-test-key")
        for content in self.drive.files.values():
            self.assertNotIn(b"rejected-private-key", content)

    def test_missing_local_reference_fails_before_selecting_runtime(self):
        self.boot()
        self.secrets.path.unlink()
        for offline in (False, True):
            self.drive.offline = offline
            restarted = self.new_cloud()
            with self.assertRaises(ConfigUnavailable) as raised:
                restarted.prepare_runtime()
            self.assertIsNone(restarted.runtime_snapshot)
            self.assertNotIn("private-test-key", str(raised.exception))
            self.assertNotIn("VALUE_", str(raised.exception))

    def test_plaintext_secrets_are_rejected_in_every_cloud_scope(self):
        for scope in ("global", "environment", "role", "node"):
            obj = self.central_object()
            layer = ({"global": obj["layers"]["global"],
                      "environment": obj["layers"]["environments"]["dev"],
                      "role": obj["layers"]["roles"]["frontend"],
                      "node": obj["layers"]["nodes"]["other-node"]["overrides"]})[scope]
            layer["LLM"] = {"Test": {"api_key": "forbidden-plaintext"}}
            self.drive.publish_object(obj, 1)
            with self.subTest(scope=scope), self.assertRaises(ConfigUnavailable):
                self.new_cloud().prepare_runtime()
        self.assertFalse((self.store.cache_dir / "desired.json").exists())

    def test_secret_reference_cannot_resolve_into_public_field(self):
        obj = self.central_object()
        obj["layers"]["global"]["prompt"] = "${secret:MASTER_KEY}"
        self.drive.publish_object(obj, 1)
        with self.assertRaises(ConfigUnavailable):
            self.store.prepare_runtime()

    def test_local_secret_replacement_remains_plaintext_and_sectioned(self):
        local = self.boot(self.local())
        result = ConfigEditor(local).update({"LLM": {"Test": {"api_key": "local-replacement-key"}}})
        with local.locked():
            self.assertEqual(local.read_unlocked()["LLM"]["Test"]["api_key"], "local-replacement-key")
        self.assertIn("local-replacement-key", (local.local.sections_dir / "providers/llm.yaml").read_text())
        self.assertNotIn("local-replacement-key", json.dumps(result))

    def test_secret_store_private_atomic_and_named_references_immutable(self):
        self.secrets.put_many({"GEMINI_API_KEY": "named-private-key"})
        self.assertEqual(self.secrets.resolve({"api_key": "${secret:GEMINI_API_KEY}"})["api_key"], "named-private-key")
        self.assertEqual(self.secrets.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.secrets.path.stat().st_mode & 0o777, 0o600)
        original = self.secrets.path.read_bytes()
        with self.assertRaises(ValueError):
            self.secrets.put_many({"GEMINI_API_KEY": "changed-private-key"})
        with patch.object(self.secrets.io, "_atomic_bytes", side_effect=OSError("Disk full")):
            with self.assertRaises(OSError):
                self.secrets.put_many({"NEW_KEY": "another-private-key"})
        self.assertEqual(self.secrets.path.read_bytes(), original)

    def test_iteration_two_legacy_defaults_stay_frozen_on_save(self):
        obj = self.central_object()
        obj["layers"]["global"] = {"defaults": self.defaults, "overrides": {"server": {"port": 8005}}}
        self.drive.publish_object(obj, 1)
        self.boot()
        original = copy.deepcopy(obj["layers"]["global"])
        self.defaults["wakeup_greeting"] = "New release greeting"
        self.default_path.write_text(yaml.safe_dump(self.defaults))
        ConfigEditor(self.store).update({"prompt": "Node edit"}, base_revision=1)
        committed = json.loads(self.drive.files[self.drive.manifest["config"]["file_id"]])
        self.assertEqual(committed["layers"]["global"], original)
        self.assertNotIn("wakeup_greeting", self.new_cloud().prepare_runtime())

    def test_secret_store_failure_prevents_remote_publication(self):
        import traceback
        self.boot()
        original = copy.deepcopy(self.drive.manifest)
        original_files = copy.deepcopy(self.drive.files)
        original_status = self.status()
        original_caches = {name: (self.store.cache_dir / name).read_bytes()
                           for name in ("desired.json", "active.json")}
        secret = "new-private-key"
        private_path = "/private/node-secrets/private-store.json"
        for error_type in (OSError, PermissionError):
            references = []

            def fail_write(values):
                references.extend(values)
                self.assertEqual(list(values.values()), [secret])
                raise error_type(f"Atomic write failed: {secret} {references[0]} {private_path}")

            with self.subTest(error_type=error_type), patch.object(self.secrets, "put_many", side_effect=fail_write):
                with self.assertRaises(ConfigUnavailable) as raised:
                    ConfigEditor(self.store).update({"LLM": {"Test": {"api_key": secret}}}, base_revision=1)
            self.assertEqual(str(raised.exception),
                             "Node-local secret storage unavailable; cloud configuration was not published")
            self.assertIsNone(raised.exception.__cause__)
            self.assertTrue(raised.exception.__suppress_context__)
            rendered_error = "".join(traceback.format_exception(raised.exception))
            self.assertTrue(references)
            for private in (secret, private_path, *references):
                self.assertNotIn(private, str(raised.exception))
                self.assertNotIn(private, rendered_error)
            self.assertEqual(self.drive.manifest, original)
            self.assertEqual(self.drive.files, original_files)
            self.assertEqual(self.drive.uploads, 0)
            status = self.status()
            self.assertEqual(status["desired_revision"], original_status["desired_revision"])
            self.assertEqual(status["active_revision"], original_status["active_revision"])
            for name, content in original_caches.items():
                self.assertEqual((self.store.cache_dir / name).read_bytes(), content)

    def test_manifest_cannot_inject_plaintext_metadata_into_cache(self):
        self.drive.manifest["api_key"] = "injected-private-key"
        with self.assertRaises(ConfigUnavailable):
            self.store.prepare_runtime()
        self.assertFalse((self.store.cache_dir / "desired.json").exists())

    def test_legacy_plaintext_cloud_source_requires_explicit_reprovisioning(self):
        self.drive.publish(self.defaults, self.overrides, 1)
        with self.assertRaises(ConfigUnavailable):
            self.store.prepare_runtime()
        self.assertFalse((self.store.cache_dir / "desired.json").exists())


class TopologyAdminTests(unittest.TestCase):
    def setUp(self):
        import importlib.util
        script = Path(__file__).resolve().parents[3] / "scripts/cloud_config_admin.py"
        spec = importlib.util.spec_from_file_location("cloud_config_admin", script)
        self.admin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.admin)
        self.fixture = CloudConfigTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.drive.publish_object(initial_cloud_object({}, "test-node"), 1)
        self.fixture.boot()

    def operate(self, operation, **options):
        return self.admin.run_operation(self.fixture.store, operation, **options)

    def object(self):
        drive = self.fixture.drive
        return json.loads(drive.files[drive.manifest["config"]["file_id"]])

    def test_admin_can_create_two_nodes_and_assign_environment_and_role(self):
        self.operate("set-global", layer={"prompt": "Shared prompt"})
        self.operate("set-environment", name="production", layer={"server": {"http_port": 8013}})
        self.operate("set-role", name="worker", layer={"LLM": {"Test": {"temperature": 0.3}}})
        self.operate("add-node", name="deskbox-1", environment="production", role="worker")
        result = self.operate("add-node", name="deskbox-2", environment="production", role="worker")
        self.assertEqual(result, {"operation": "add-node", "old_revision": 5, "new_revision": 6})
        self.operate("set-node", name="test-node", environment="production", role="worker",
                     layer={"prompt": "Node prompt"})
        layers = self.object()["layers"]
        for name in ("deskbox-1", "deskbox-2", "test-node"):
            self.assertEqual(layers["nodes"][name]["environment"], "production")
            self.assertEqual(layers["nodes"][name]["role"], "worker")
        self.assertEqual(self.fixture.status()["active_revision"], 1)
        self.assertEqual(self.fixture.status()["desired_revision"], 7)

    def test_admin_conflicting_writer_cannot_overwrite(self):
        fixture = self.fixture
        rival = GoogleDriveConfigStore(fixture.bootstrap, transport=fixture.drive,
            cache_dir=fixture.directory / "rival-cache", default_path=str(fixture.default_path),
            secret_provider=fixture.secrets)
        fixture.drive.before_commit = lambda: self.admin.run_operation(
            rival, "set-global", layer={"prompt": "Winning writer"})
        with self.assertRaises(ConfigConflict):
            self.operate("set-global", layer={"prompt": "Losing writer"})
        self.assertEqual(fixture.drive.manifest["revision"], 2)
        self.assertEqual(self.object()["layers"]["global"]["prompt"], "Winning writer")
        self.assertEqual(fixture.drive.uploads, 2)

    def test_admin_stale_base_revision_rejected_without_upload(self):
        self.operate("set-global", layer={"prompt": "First writer"})
        with self.assertRaises(ConfigConflict):
            self.operate("add-node", name="stale-node", base_revision=1)
        self.assertEqual(self.fixture.drive.uploads, 1)
        self.assertNotIn("stale-node", self.object()["layers"]["nodes"])

    def test_admin_referenced_role_and_environment_deletion_rejected(self):
        self.operate("set-environment", name="production", layer={})
        self.operate("set-role", name="worker", layer={})
        self.operate("add-node", name="deskbox-1", environment="production", role="worker")
        before = copy.deepcopy(self.fixture.drive.manifest)
        uploads = self.fixture.drive.uploads
        for operation, name in (("delete-environment", "production"), ("delete-role", "worker")):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                self.operate(operation, name=name)
        self.assertEqual(self.fixture.drive.manifest, before)
        self.assertEqual(self.fixture.drive.uploads, uploads)

    def test_admin_rejects_current_node_and_requires_deassignment_before_delete(self):
        with self.assertRaises(ValueError):
            self.operate("delete-node", name="test-node")
        self.operate("set-role", name="worker", layer={})
        self.operate("add-node", name="deskbox-1", role="worker")
        with self.assertRaises(ValueError):
            self.operate("delete-node", name="deskbox-1")
        self.operate("set-node", name="deskbox-1", role="-")
        self.operate("delete-node", name="deskbox-1")
        self.assertEqual(set(self.object()["layers"]["nodes"]), {"test-node"})
        self.operate("delete-role", name="worker")
        self.assertEqual(self.object()["layers"]["roles"], {})

    def test_admin_validates_other_nodes_before_upload(self):
        self.operate("set-role", name="worker", layer={})
        self.operate("add-node", name="deskbox-1", role="worker")
        before = copy.deepcopy(self.fixture.drive.manifest)
        uploads = self.fixture.drive.uploads
        with self.assertRaises(ValueError):
            self.operate("set-role", name="worker", layer={"server": {"port": 0}})
        self.assertEqual(self.fixture.drive.manifest, before)
        self.assertEqual(self.fixture.drive.uploads, uploads)

    def test_admin_rejects_plaintext_secrets_and_bootstrap_overrides(self):
        for layer in ({"LLM": {"Test": {"api_key": "not-for-cloud"}}},
                      {"node_id": "remote-identity"}, {"google_drive": {}},
                      {"config_provider": "local"}, {"credentials_path": "private.json"}):
            with self.subTest(layer=layer), self.assertRaises(ValueError):
                self.operate("set-global", layer=layer)
        self.assertEqual(self.fixture.drive.uploads, 0)

    def test_admin_show_is_reference_only_and_does_not_publish(self):
        self.operate("set-node", name="test-node", layer=self.fixture.cloud_overrides)
        uploads = self.fixture.drive.uploads
        result = self.operate("show")
        self.assertEqual(result["revision"], 2)
        self.assertIn("${secret:", json.dumps(result))
        self.assertNotIn("private-test-key", json.dumps(result))
        self.assertEqual(self.fixture.drive.uploads, uploads)

    def test_admin_legacy_sources_show_but_require_explicit_reprovision_for_mutation(self):
        self.fixture.drive.publish(self.fixture.defaults, self.fixture.cloud_overrides, 2)
        self.assertEqual(self.operate("show")["revision"], 2)
        with self.assertRaises(ValueError):
            self.operate("add-node", name="deskbox-1")
        self.assertEqual(self.fixture.drive.uploads, 0)

    def test_cli_adds_two_assigned_nodes(self):
        import io
        self.operate("set-environment", name="production", layer={})
        self.operate("set-role", name="worker", layer={})
        for name in ("deskbox-1", "deskbox-2"):
            output = io.StringIO()
            with patch.object(self.admin, "load_bootstrap", return_value=self.fixture.bootstrap), \
                 patch("sys.stdout", output):
                code = self.admin.main(["add-node", name, "--environment", "production", "--role", "worker"],
                                       store_factory=lambda bootstrap: self.fixture.store)
            self.assertEqual(code, 0)
            report = json.loads(output.getvalue())
            self.assertEqual(report["new_revision"], report["old_revision"] + 1)
            self.assertEqual(self.object()["layers"]["nodes"][name],
                             {"environment": "production", "role": "worker", "overrides": {}})

    def test_local_secret_cli_performs_no_cloud_request(self):
        import io
        output = io.StringIO()
        with patch.object(self.admin, "load_bootstrap", return_value=self.fixture.bootstrap), \
             patch.object(self.admin, "LocalSecretStore", return_value=self.fixture.secrets), \
             patch.object(self.admin.getpass, "getpass", return_value="named-cli-private-key"), \
             patch("sys.stdin.isatty", return_value=True), \
             patch("sys.stdout", output), \
             patch.object(self.fixture.drive, "read_manifest") as read:
            code = self.admin.main(["set-local-secret", "GEMINI_API_KEY"])
        self.assertEqual(code, 0)
        read.assert_not_called()
        self.assertEqual(self.fixture.secrets.get("GEMINI_API_KEY"), "named-cli-private-key")
        self.assertNotIn("named-cli-private-key", output.getvalue())
        self.assertEqual(self.fixture.drive.uploads, 0)

    def test_cli_parser_dispatch_and_revision_report(self):
        import io
        layer = self.fixture.directory / "role.yaml"
        layer.write_text("LLM:\n  Test:\n    temperature: 0.2\n")
        output = io.StringIO()
        with patch.object(self.admin, "load_bootstrap", return_value=self.fixture.bootstrap), \
             patch("sys.stdout", output):
            code = self.admin.main(["--base-revision", "1", "set-role", "worker", "--file", str(layer)],
                                   store_factory=lambda bootstrap: self.fixture.store)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), {"operation": "set-role", "old_revision": 1, "new_revision": 2})
        self.assertEqual(self.object()["layers"]["roles"]["worker"]["LLM"]["Test"]["temperature"], 0.2)
        errors = io.StringIO()
        with patch.object(self.admin, "load_bootstrap", return_value=self.fixture.bootstrap), \
             patch("sys.stderr", errors):
            code = self.admin.main(["--base-revision", "1", "add-node", "stale-node"],
                                   store_factory=lambda bootstrap: self.fixture.store)
        self.assertEqual(code, 3)
        self.assertIn("Conflict", errors.getvalue())
        self.assertEqual(self.fixture.drive.manifest["revision"], 2)


class SettingsApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from unittest.mock import Mock
        self.fixture = CloudConfigTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.boot()
        # Prevent legacy module-level logging from loading deployment config.
        with patch("config.logger.setup_logging", return_value=Mock()):
            from core.api.settings_handler import SettingsHandler
        self.handler = SettingsHandler.__new__(SettingsHandler)
        self.handler.editor = ConfigEditor(self.fixture.store)
        self.handler.allow_remote = False
        self.handler.restart_required = False
        self.handler.soundbank_cleanup = None
        self.handler.soundbank = Mock()
        self.handler.soundbank.runtime_directory.return_value = "data/soundbank"
        self.handler.soundbank.runtime_audio_contract.return_value = {"codec": "opus"}

    def request(self, body):
        from unittest.mock import AsyncMock, Mock
        request = Mock()
        request.content_type = "application/json"
        request.transport.get_extra_info.return_value = ("127.0.0.1", 1234)
        request.json = AsyncMock(return_value=body)
        return request

    async def test_http_secret_storage_failure_returns_redacted_503_without_publication(self):
        fixture = self.fixture
        original_manifest = copy.deepcopy(fixture.drive.manifest)
        original_files = copy.deepcopy(fixture.drive.files)
        original_caches = {name: (fixture.store.cache_dir / name).read_bytes()
                           for name in ("desired.json", "active.json")}
        secret = "api-replacement-private-key"
        private_path = "/private/node-secrets/api-private-store.json"
        references = []

        def fail_write(values):
            references.extend(values)
            self.assertEqual(list(values.values()), [secret])
            raise OSError(f"Atomic write failed: {secret} {references[0]} {private_path}")

        with patch.object(fixture.secrets, "put_many", side_effect=fail_write):
            response = await self.handler.handle_put(self.request({
                "config": {"LLM": {"Test": {"api_key": secret}}}, "base_revision": 1,
            }))
        self.assertEqual(response.status, 503)
        payload = json.loads(response.text)
        self.assertEqual(set(payload), {"error", "configuration_source"})
        self.assertEqual(payload["error"],
                         "Node-local secret storage unavailable; cloud configuration was not published")
        self.assertTrue(references)
        for private in (secret, private_path, "Atomic write failed", "private-test-key", *references):
            self.assertNotIn(private, response.text)
        self.assertNotIn("${secret:", response.text)
        self.assertEqual(payload["configuration_source"]["desired_revision"], 1)
        self.assertEqual(payload["configuration_source"]["active_revision"], 1)
        self.assertEqual(fixture.drive.manifest, original_manifest)
        self.assertEqual(fixture.drive.files, original_files)
        self.assertEqual(fixture.drive.uploads, 0)
        for name, content in original_caches.items():
            self.assertEqual((fixture.store.cache_dir / name).read_bytes(), content)

    async def test_http_save_returns_409_with_revision_and_conflict_metadata(self):
        self.fixture.drive.publish(self.fixture.defaults, self.fixture.cloud_overrides, 2)
        response = await self.handler.handle_put(self.request({"config": {"server": {"port": 8001}}, "base_revision": 1}))
        self.assertEqual(response.status, 409)
        source = json.loads(response.text)["configuration_source"]
        self.assertEqual(source["desired_revision"], 2)
        self.assertEqual(source["active_revision"], 1)
        self.assertTrue(source["conflict"])
        self.assertNotIn("private-test-key", response.text)

    async def test_http_manifest_read_race_returns_409_for_save_and_sync(self):
        self.fixture.drive.manifest_read_race = True
        response = await self.handler.handle_put(self.request({"config": {"server": {"port": 8001}}, "base_revision": 1}))
        self.assertEqual(response.status, 409)
        self.assertTrue(json.loads(response.text)["configuration_source"]["conflict"])
        response = await self.handler.handle_sync(self.request({}))
        self.assertEqual(response.status, 409)
        self.assertEqual(self.fixture.drive.uploads, 0)

    async def test_http_cloud_save_does_not_restart_or_apply(self):
        from unittest.mock import Mock
        self.handler.request_restart = Mock()
        response = await self.handler.handle_put(self.request({"config": {"server": {"port": 8001}}, "base_revision": 1}))
        payload = json.loads(response.text)
        self.assertEqual(response.status, 200)
        self.assertTrue(payload["restart_required"])
        self.assertEqual(payload["configuration_source"]["active_revision"], 1)
        self.handler.request_restart.assert_not_called()

    async def test_http_get_masks_all_configured_secrets(self):
        response = await self.handler.handle_get(self.request({}))
        self.assertEqual(response.status, 200)
        for secret in ("private-test-key", "private-test-header", "private-credentials.json"):
            self.assertNotIn(secret, response.text)
        self.assertIn("configuration_source", json.loads(response.text))
        self.assertEqual(response.headers["Cache-Control"], "no-store, max-age=0")

    async def test_http_sync_updates_desired_without_applying(self):
        self.fixture.drive.publish(self.fixture.defaults, self.fixture.cloud_overrides, 2)
        response = await self.handler.handle_sync(self.request({}))
        source = json.loads(response.text)["configuration_source"]
        self.assertEqual(response.status, 200)
        self.assertEqual((source["desired_revision"], source["active_revision"]), (2, 1))

    async def test_local_diagnostic_only_save_retains_no_restart_behavior(self):
        self.handler.editor = ConfigEditor(self.fixture.boot(self.fixture.local()))
        response = await self.handler.handle_put(self.request({"config": {"server": {
            "settings": {"diagnostics": {"thresholds_ms": {"total": 30000}}}}}}))
        self.assertEqual(response.status, 200)
        self.assertFalse(json.loads(response.text)["restart_required"])


class DriveTransportTests(unittest.TestCase):
    def test_manifest_update_uses_if_match_and_reports_precondition_failure(self):
        from unittest.mock import Mock
        session = Mock()
        session.request.return_value.status_code = 412
        transport = GoogleDriveTransport("unused.json", session=session)
        with self.assertRaises(ConfigConflict):
            transport.replace_manifest("manifest", b"{}", '"etag-1"')
        request = session.request.call_args
        self.assertEqual(request.kwargs["headers"]["If-Match"], '"etag-1"')
        self.assertEqual(request.kwargs["timeout"], 20)

    def test_etag_change_during_manifest_read_raises_conflict(self):
        from unittest.mock import Mock
        session = Mock()
        before, media, after = Mock(), Mock(), Mock()
        for response in (before, media, after):
            response.status_code = 200
        before.json.return_value = {"id": "manifest", "etag": '"old"'}
        media.content = b"{}"
        after.json.return_value = {"id": "manifest", "etag": '"new"'}
        session.request.side_effect = [before, media, after]
        transport = GoogleDriveTransport("unused.json", session=session)
        with self.assertRaises(ConfigConflict):
            transport.read_manifest("manifest")

    def test_create_app_owned_folder_keeps_drive_file_scope(self):
        from unittest.mock import Mock
        session = Mock()
        session.request.return_value.status_code = 200
        session.request.return_value.json.return_value = {"id": "app-folder"}
        transport = GoogleDriveTransport("unused.json", session=session)
        self.assertEqual(transport.create_folder("Xiaozhi Config"), "app-folder")
        request = session.request.call_args
        self.assertEqual(request.args[0], "POST")
        self.assertEqual(request.kwargs["json"], {"title": "Xiaozhi Config", "mimeType": "application/vnd.google-apps.folder"})
        self.assertEqual(transport.SCOPES, ["https://www.googleapis.com/auth/drive.file"])

    def test_transport_errors_do_not_expose_credentials_or_remote_response(self):
        from unittest.mock import Mock
        session = Mock()
        session.request.side_effect = RuntimeError("private-test-token")
        transport = GoogleDriveTransport("unused.json", session=session)
        with self.assertRaises(ConfigUnavailable) as raised:
            transport.download("object")
        self.assertNotIn("private-test-token", str(raised.exception))


class ProvisioningTests(unittest.TestCase):
    def setUp(self):
        import importlib.util
        script = Path(__file__).resolve().parents[3] / "scripts/init_cloud_config.py"
        spec = importlib.util.spec_from_file_location("init_cloud_config", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.provision = module.provision_cloud_source
        self.fixture = CloudConfigTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_provision_creates_app_folder_and_shared_topology_without_switch(self):
        fixture = self.fixture
        original = copy.deepcopy(fixture.drive.manifest)
        source = self.provision(fixture.drive, fixture.defaults, fixture.overrides, "test-node",
                                folder_name="Xiaozhi Config", secret_provider=fixture.secrets)
        self.assertEqual(fixture.drive.created_folders, ["Xiaozhi Config"])
        self.assertEqual(fixture.drive.upload_folders, ["created-folder", "created-folder"])
        manifest = json.loads(fixture.drive.files[source["manifest_file_id"]])
        obj = json.loads(fixture.drive.files[manifest["config"]["file_id"]])
        self.assertEqual(set(obj["layers"]), {"global", "environments", "roles", "nodes"})
        self.assertEqual(obj["layers"]["global"], {})
        self.assertEqual(fixture.secrets.resolve(obj["layers"]["nodes"]["test-node"]["overrides"]), fixture.overrides)
        self.assertNotIn("private-test-key", json.dumps(obj))
        self.assertNotIn("private-test-header", json.dumps(obj))
        self.assertEqual(source["folder_id"], "created-folder")
        self.assertEqual(fixture.drive.manifest, original)

    def test_provision_existing_accessible_folder_does_not_create_another(self):
        fixture = self.fixture
        self.provision(fixture.drive, fixture.defaults, fixture.overrides, "test-node", folder_id="existing-folder", secret_provider=fixture.secrets)
        self.assertEqual(fixture.drive.created_folders, [])
        self.assertEqual(fixture.drive.upload_folders, ["existing-folder", "existing-folder"])

    def test_provision_secret_write_failure_creates_no_remote_resources(self):
        fixture = self.fixture
        with patch.object(fixture.secrets, "put_many", side_effect=OSError("Private store unavailable")):
            with self.assertRaises(OSError):
                self.provision(fixture.drive, fixture.defaults, fixture.overrides, "test-node",
                               folder_name="Xiaozhi Config", secret_provider=fixture.secrets)
        self.assertEqual(fixture.drive.created_folders, [])
        self.assertEqual(fixture.drive.uploads, 0)

    def test_provision_does_not_freeze_software_defaults(self):
        fixture = self.fixture
        fixture.defaults["wakeup_greeting"] = "Release-owned greeting"
        source = self.provision(fixture.drive, fixture.defaults, fixture.overrides, "test-node",
                                folder_name="Xiaozhi Config", secret_provider=fixture.secrets)
        manifest = json.loads(fixture.drive.files[source["manifest_file_id"]])
        content = fixture.drive.files[manifest["config"]["file_id"]]
        self.assertNotIn(b"Release-owned greeting", content)
        self.assertNotIn(b'"defaults"', content)
        self.assertNotIn(b"private-test-key", content)
        self.assertNotIn(b"private-test-header", content)

    def test_invalid_config_is_rejected_before_folder_creation(self):
        fixture = self.fixture
        with self.assertRaises(ValueError):
            self.provision(fixture.drive, fixture.defaults, {"server": {"port": 0}}, "test-node",
                           folder_name="Xiaozhi Config", secret_provider=fixture.secrets)
        self.assertEqual(fixture.drive.created_folders, [])
        self.assertEqual(fixture.drive.uploads, 0)


if __name__ == "__main__":
    unittest.main()
