"""Full-state provisioning against a fake existing Drive source; no network."""

import copy
import importlib.util
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from config.bootstrap import load_bootstrap, save_bootstrap
from config.cloud_layers import resolve_layers
from config.cloud_provisioning import (
    BootstrapPublicationError, ProvisioningConflict, ProvisioningError,
    ProvisioningReconciliation, ProvisioningRecoveryRequired, ProvisioningWriterMismatch,
    provision_cloud_state, semantic_config,
)
from config.config_loader import merge_configs
from config.config_store import canonical_bytes, checksum, stage_provider_switch
from config.drive_transport import GoogleDriveTransport
from config.google_drive_config import GoogleDriveConfigStore
from core.soundbank import soundbank_entry_optimized
import test_cloud_config as config_tests
from test_cloud_memory import entry
from test_cloud_soundbank import AssetDrive, p3_bytes, wav_bytes

PRIVATE = "private-key /private/credentials.json robot-a Camera uses shared I2C"


class ProvisionDrive(AssetDrive):
    def __init__(self, defaults, overrides):
        super().__init__(defaults, overrides)
        self.memory_manifests = []
        self.memory_etags = {}
        self.fail_folder = False
        self.fail_memory = None
        self.fail_manifest_download_once = False
        self.after_config = None

    def validate_folder(self, folder_id):
        self.events.append(("folder", folder_id))
        if self.fail_folder:
            raise OSError(PRIVATE)

    def read_manifest(self, file_id):
        if file_id == "manifest":
            return super().read_manifest(file_id)
        return self.download(file_id), str(self.memory_etags.get(file_id, 1))

    def upload_immutable(self, folder_id, content, name):
        if name.startswith("memory-"):
            if (self.fail_memory == "snapshot" and name != "memory-manifest.json"
                    or self.fail_memory == "manifest" and name == "memory-manifest.json"):
                raise OSError(PRIVATE)
        file_id = super().upload_immutable(folder_id, content, name)
        if name == "memory-manifest.json":
            self.memory_manifests.append(file_id)
            self.memory_etags[file_id] = 1
            if self.fail_memory == "manifest-response":
                raise OSError(PRIVATE)
        elif name.startswith("memory-") and self.fail_memory == "verify_snapshot":
            self.files[file_id] = b"corrupt snapshot"
        return file_id

    def download(self, file_id):
        if file_id in self.memory_manifests and self.fail_manifest_download_once:
            self.fail_manifest_download_once = False
            raise OSError(PRIVATE)
        return super().download(file_id)

    def replace_manifest(self, file_id, content, etag):
        if file_id == "manifest":
            super().replace_manifest(file_id, content, etag)
            if self.after_config:
                self.after_config()
        else:
            if etag != str(self.memory_etags[file_id]):
                from config.config_store import ConfigConflict
                raise ConfigConflict(PRIVATE)
            self.files[file_id] = content
            self.memory_etags[file_id] += 1

    def seed_memory(self, scopes, writer="test-node", revision=7):
        snapshot = canonical_bytes({"schema_version": 1, "scopes": scopes})
        self.files["existing-memory-snapshot"] = snapshot
        manifest = {"schema_version": 1, "revision": revision, "writer_node_id": writer,
                    "snapshot": {"file_id": "existing-memory-snapshot", "sha256": checksum(snapshot)}}
        identifier = "existing-memory-manifest"
        self.files[identifier] = canonical_bytes(manifest)
        self.memory_etags[identifier] = 1
        return identifier


class FullStateProvisioningTests(unittest.TestCase):
    def setUp(self):
        fixture = config_tests.CloudConfigTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.directory = fixture.directory
        self.root = self.directory / "soundbank"
        self.memory_path = self.directory / "custom-memory.yaml"
        self.scopes = {"robot-a": [entry(), entry("inactive", "Old device fact", active=False)],
                       "robot-b": [entry("second-device", "Second robot fact", pinned=True)],
                       "empty-device": []}
        self.memory_path.write_text(yaml.safe_dump(self.scopes))
        fixture.defaults["selected_module"]["Memory"] = "ExplicitAlias"
        fixture.defaults["Memory"] = {"ExplicitAlias": {"type": "mem_local_explicit",
                                                        "path": str(self.memory_path), "entry_max_chars": 500}}
        fixture.defaults["asr_min_audio_ms"] = 300
        fixture.defaults["xiaozhi"] = {"audio_params": {"sample_rate": 24000}}
        fixture.defaults["static_soundbank"]["entries"] = {
            "Shared": {"file": "shared.wav", "text": "Shared sound"},
            "Repo only": {"file": "repo.p3"},
        }
        fixture.overrides["static_soundbank"]["entries"] = {
            "Hello": {"file": "hello.wav", "text": "Hello robot", "optimized": {
                "file": "hello.p3", "codec": "opus", "sample_rate": 24000,
                "channels": 1, "frame_duration_ms": 60}},
            "MP3": "retained.mp3", "Canonical P3": {"file": "canonical.p3"},
        }
        fixture.default_path.write_text(yaml.safe_dump(fixture.defaults))
        for name, content in {"hello.wav": wav_bytes(0), "shared.wav": wav_bytes(1),
                              "hello.p3": p3_bytes(2), "canonical.p3": p3_bytes(3),
                              "repo.p3": p3_bytes(4), "retained.mp3": b"ID3 retained test MP3"}.items():
            (self.root / name).write_bytes(content)
        cloud_overrides, pending = fixture.secrets.externalize(fixture.overrides)
        fixture.secrets.put_many(pending)
        self.drive = ProvisionDrive(fixture.defaults, cloud_overrides)
        self.obj = {"schema_version": 1, "layers": {
            "global": {"server": {"port": 8100}, "static_soundbank": {"entries": {"Shared": {"file": "shared.wav"}}}},
            "environments": {"dev": {"server": {"http_port": 8004}}},
            "roles": {"server": {"asr_min_audio_ms": 500}},
            "nodes": {
                "test-node": {"environment": "dev", "role": "server", "overrides": cloud_overrides},
                "reader-node": {"environment": "dev", "role": "server", "overrides": {
                    "server": {"port": 8012}, "LLM": {"Test": {"api_key": "${secret:READER_KEY}"}},
                    "static_soundbank": {"entries": {"Shared": {"file": "reader.wav"}}}}},
            },
        }}
        self.drive.publish_object(self.obj, 4)
        self.local = fixture.local()
        with self.local.locked():
            self.local.commit_unlocked(fixture.overrides)  # Exercise config.d source semantics.
        self.bootstrap = {**copy.deepcopy(fixture.bootstrap), "config_provider": "local"}
        self.bootstrap_path = self.directory / "bootstrap.yaml"
        save_bootstrap(self.bootstrap, self.bootstrap_path)
        self.local.bootstrap = copy.deepcopy(self.bootstrap)
        self.receipt_path = self.directory / "provisioning/receipt.json"
        self.cache_dir = self.directory / "cloud-config"

    def run_provision(self):
        return provision_cloud_state(self.local, bootstrap_path=self.bootstrap_path, transport=self.drive,
                                     secret_provider=self.fixture.secrets, receipt_path=self.receipt_path,
                                     runtime_cache_dir=self.cache_dir)

    def local_files(self):
        return {str(path): path.read_bytes() for directory in (self.local.local.directory, self.root)
                for path in directory.rglob("*") if path.is_file() and not path.name.endswith(".lock")} | {
                    str(self.memory_path): self.memory_path.read_bytes()}

    def current_object(self):
        return json.loads(self.drive.files[self.drive.manifest["config"]["file_id"]])

    def effective(self, node="test-node"):
        defaults, overrides = resolve_layers(self.current_object(), node, self.fixture.defaults)
        return merge_configs(defaults, overrides)

    def assert_local_unchanged(self, before):
        self.assertEqual(self.local_files(), before)
        self.assertEqual(load_bootstrap(self.bootstrap_path)["config_provider"], "local")
        self.assertEqual(self.local.bootstrap["config_provider"], "local")
        self.assertIsNone(self.local.pending_provider)
        self.assertFalse((self.directory / "cloud-memory").exists())
        self.assertFalse((self.directory / "cloud-soundbank").exists())

    def test_existing_source_full_state_bootstrap_and_readiness_without_materialization(self):
        before = self.local_files()
        with patch("config.cloud_soundbank.CloudSoundbankAssets.materialize", side_effect=AssertionError("Materialize")), \
                patch("config.cloud_memory.CloudMemoryStore.sync", side_effect=AssertionError("Memory sync")), \
                patch("config.config_store.stage_provider_switch", side_effect=AssertionError("Switch")):
            result = self.run_provision()
        self.assertEqual(result.config_revision, 5)
        self.assertEqual(result.memory_revision, 1)
        self.assertFalse(result.memory_reused)
        self.assert_local_unchanged(before)
        bootstrap = load_bootstrap(self.bootstrap_path)
        self.assertEqual(bootstrap["node_id"], "test-node")
        self.assertEqual(bootstrap["google_drive"]["folder_id"], "folder")
        self.assertEqual(bootstrap["google_drive"]["manifest_file_id"], "manifest")
        manifest = json.loads(self.drive.files[bootstrap["google_drive"]["memory_manifest_file_id"]])
        self.assertEqual(manifest["writer_node_id"], "test-node")
        self.assertEqual(manifest["revision"], 1)
        snapshot = self.drive.files[manifest["snapshot"]["file_id"]]
        self.assertEqual(checksum(snapshot), manifest["snapshot"]["sha256"])
        self.assertEqual(snapshot, canonical_bytes({"schema_version": 1, "scopes": self.scopes}))
        self.assertNotIn("test-node", json.loads(snapshot)["scopes"])
        actual = self.fixture.secrets.resolve(semantic_config(self.effective()))
        with self.local.locked():
            layers = self.local.source_layers_unlocked()
        self.assertEqual(actual, semantic_config(merge_configs(layers["defaults"], layers["overrides"])))
        # The actual Settings source-switch path passes after provisioning.
        candidate = GoogleDriveConfigStore({**bootstrap, "config_provider": "google_drive"},
                                          transport=self.drive, secret_provider=self.fixture.secrets,
                                          default_path=self.fixture.default_path, cache_dir=self.cache_dir)
        with patch("config.config_store.get_config_store", return_value=self.local), \
                patch("config.config_store.load_bootstrap", return_value=bootstrap), \
                patch("config.config_store.create_config_store", return_value=candidate), \
                patch("config.config_store.save_bootstrap") as save:
            stage_provider_switch("google_drive")
            save.assert_called_once()
        self.assertEqual(self.local_files(), before)

    def test_shared_topology_and_cross_node_file_owners_preserved(self):
        self.run_provision()
        obj = self.current_object()
        self.assertEqual(obj["layers"]["environments"], self.obj["layers"]["environments"])
        self.assertEqual(obj["layers"]["roles"], self.obj["layers"]["roles"])
        self.assertEqual(obj["layers"]["nodes"]["reader-node"], self.obj["layers"]["nodes"]["reader-node"])
        global_layer = obj["layers"]["global"]
        self.assertEqual(semantic_config(global_layer), self.obj["layers"]["global"])
        self.assertIn("cloud", global_layer["static_soundbank"]["entries"]["Shared"])
        node_entries = obj["layers"]["nodes"]["test-node"]["overrides"]["static_soundbank"]["entries"]
        self.assertNotIn("Shared", node_entries)
        self.assertIn("cloud", node_entries["Repo only"])
        reader = self.effective("reader-node")
        self.assertEqual(reader["static_soundbank"]["entries"]["Shared"]["file"], "reader.wav")
        self.assertNotIn("cloud", reader["static_soundbank"]["entries"]["Shared"])
        self.assertEqual(reader["server"]["port"], 8012)
        self.assertEqual(self.effective()["server"]["port"], 8000)

    def test_all_retained_formats_optimized_and_secrets_verified(self):
        self.run_provision()
        entries = self.effective()["static_soundbank"]["entries"]
        for value in entries.values():
            for asset in (value, soundbank_entry_optimized(value)):
                if asset is None:
                    continue
                pointer = asset["cloud"]
                content = self.drive.files[pointer["file_id"]]
                self.assertEqual(content, (self.root / asset["file"]).read_bytes())
                self.assertEqual(pointer["sha256"], checksum(content))
                self.assertEqual(pointer["size"], len(content))
        self.assertEqual({mime for _, _, mime in self.drive.blobs},
                         {"audio/wav", "audio/mpeg", "application/octet-stream"})
        raw = canonical_bytes(self.current_object())
        self.assertNotIn(b"private-test-key", raw)
        self.assertNotIn(b"private-test-header", raw)
        self.assertIn(b"${secret:", raw)

    def test_identical_rerun_reuses_memory_and_config_without_new_uploads(self):
        self.run_provision()
        before = (copy.deepcopy(self.drive.manifest), self.drive.uploads, len(self.drive.memory_manifests))
        result = self.run_provision()
        self.assertTrue(result.memory_reused)
        self.assertEqual(result.memory_revision, 1)
        self.assertEqual((self.drive.manifest, self.drive.uploads, len(self.drive.memory_manifests)), before)

    def existing_memory(self, scopes=None, *, writer="test-node", revision=7):
        identifier = self.drive.seed_memory(self.scopes if scopes is None else scopes, writer, revision)
        self.bootstrap["google_drive"]["memory_manifest_file_id"] = identifier
        save_bootstrap(self.bootstrap, self.bootstrap_path)
        return identifier

    def test_matching_existing_memory_revision_is_reused_without_reset(self):
        identifier = self.existing_memory(revision=9)
        before = self.drive.files[identifier]
        result = self.run_provision()
        self.assertEqual(result.memory_revision, 9)
        self.assertTrue(result.memory_reused)
        self.assertEqual(self.drive.files[identifier], before)
        self.assertEqual(self.drive.memory_manifests, [])

    def test_existing_memory_differences_writer_invalid_or_missing_fail_before_uploads(self):
        variants = ("different", "writer", "corrupt", "missing", "invalid-id", "empty-id")
        for variant in variants:
            with self.subTest(variant=variant):
                scopes = {"robot-a": [entry(content="Different memory")]} if variant == "different" else self.scopes
                identifier = self.existing_memory(scopes, writer="other-writer" if variant == "writer" else "test-node")
                if variant == "corrupt":
                    self.drive.files[identifier] = b"invalid"
                elif variant == "missing":
                    del self.drive.files[identifier]
                elif variant in {"invalid-id", "empty-id"}:
                    self.bootstrap["google_drive"]["memory_manifest_file_id"] = "invalid/id" if variant == "invalid-id" else ""
                    save_bootstrap(self.bootstrap, self.bootstrap_path)
                before = (self.local_files(), self.bootstrap_path.read_bytes(), copy.deepcopy(self.drive.manifest), self.drive.uploads)
                expected = ProvisioningWriterMismatch if variant == "writer" else ProvisioningError
                with self.assertRaises(expected):
                    self.run_provision()
                self.assertEqual((self.local_files(), self.bootstrap_path.read_bytes(), self.drive.manifest, self.drive.uploads), before)

    def test_local_memory_malformed_or_lossy_and_bad_p3_fail_before_uploads(self):
        original_memory = self.memory_path.read_bytes()
        variants = ("malformed", "truncated", "legacy", "p3", "missing-asset", "empty-asset")
        for variant in variants:
            with self.subTest(variant=variant):
                self.memory_path.write_bytes(original_memory)
                asset = self.root / "canonical.p3"
                asset.write_bytes(p3_bytes(3))
                if variant == "malformed":
                    self.memory_path.write_text("bad: [yaml")
                elif variant == "truncated":
                    self.memory_path.write_text(yaml.safe_dump({"robot-a": [dict(entry(), content="x" * 501)]}))
                elif variant == "legacy":
                    self.memory_path.write_text(yaml.safe_dump({"robot-a": [{"content": "Missing metadata"}]}))
                elif variant == "p3":
                    asset.write_bytes(b"invalid P3")
                elif variant == "missing-asset":
                    asset.unlink()
                else:
                    asset.write_bytes(b"")
                before = (self.bootstrap_path.read_bytes(), self.drive.uploads, copy.deepcopy(self.drive.manifest))
                with self.assertRaises(ProvisioningError):
                    self.run_provision()
                self.assertEqual((self.bootstrap_path.read_bytes(), self.drive.uploads, self.drive.manifest), before)

    def test_folder_access_and_node_assignment_preflight_fail_before_remote_mutation(self):
        self.drive.fail_folder = True
        with self.assertRaises(ProvisioningError):
            self.run_provision()
        self.drive.fail_folder = False
        obj = copy.deepcopy(self.obj)
        del obj["layers"]["nodes"]["test-node"]
        self.drive.publish_object(obj, 5)
        before = copy.deepcopy(self.drive.manifest)
        with self.assertRaises(ProvisioningError):
            self.run_provision()
        self.assertEqual(self.drive.uploads, 0)
        self.assertEqual(self.drive.manifest, before)

    def test_soundbank_upload_and_config_upload_failures_leave_bootstrap_memory_untouched(self):
        before = (self.local_files(), self.bootstrap_path.read_bytes(), copy.deepcopy(self.drive.manifest))
        self.drive.fail_blob = True
        with self.assertRaises(ProvisioningError):
            self.run_provision()
        self.assertEqual((self.local_files(), self.bootstrap_path.read_bytes(), self.drive.manifest), before)
        self.drive.fail_blob = False
        self.drive.fail_upload = True
        with self.assertRaises(ProvisioningError):
            self.run_provision()
        self.assertEqual((self.local_files(), self.bootstrap_path.read_bytes(), self.drive.manifest), before)
        self.assertEqual(self.drive.memory_manifests, [])

    def test_config_cas_race_keeps_winning_manifest_without_memory_creation(self):
        winner = []

        def race():
            obj = copy.deepcopy(self.obj)
            obj["layers"]["nodes"]["reader-node"]["overrides"]["server"]["port"] = 8020
            self.drive.publish_object(obj, 5)
            winner.append(copy.deepcopy(self.drive.manifest))

        self.drive.before_commit = race
        before = (self.local_files(), self.bootstrap_path.read_bytes())
        with self.assertRaises(ProvisioningConflict):
            self.run_provision()
        self.assertEqual(self.drive.manifest, winner[0])
        self.assertEqual((self.local_files(), self.bootstrap_path.read_bytes()), before)
        self.assertEqual(self.drive.memory_manifests, [])

    def test_memory_seed_failure_after_config_success_and_rerun_converges(self):
        before = self.local_files(), self.bootstrap_path.read_bytes()
        self.drive.fail_memory = "snapshot"
        with self.assertRaises(ProvisioningError):
            self.run_provision()
        self.assertEqual(self.drive.manifest["revision"], 5)
        self.assertEqual((self.local_files(), self.bootstrap_path.read_bytes()), before)
        self.drive.fail_memory = None
        result = self.run_provision()
        self.assertEqual(result.config_revision, 5)
        self.assertEqual(result.memory_revision, 1)
        self.assertEqual(len(self.drive.memory_manifests), 1)

    def test_memory_snapshot_verify_or_manifest_creation_failure_never_publishes_bootstrap(self):
        for failure in ("verify_snapshot", "manifest"):
            with self.subTest(failure=failure):
                self.drive.fail_memory = failure
                before = self.bootstrap_path.read_bytes()
                with self.assertRaises(ProvisioningError):
                    self.run_provision()
                self.assertEqual(self.bootstrap_path.read_bytes(), before)
                self.assertEqual(self.drive.memory_manifests, [])

    def test_manifest_verify_transient_failure_recovers_known_id_without_second_authority(self):
        before = self.bootstrap_path.read_bytes()
        self.drive.fail_manifest_download_once = True
        with self.assertRaises(ProvisioningError):
            self.run_provision()
        self.assertEqual(self.bootstrap_path.read_bytes(), before)
        self.assertTrue(self.receipt_path.exists())
        identifier = self.drive.memory_manifests[0]
        result = self.run_provision()
        self.assertTrue(result.memory_reused)
        self.assertEqual(self.drive.memory_manifests, [identifier])
        self.assertEqual(load_bootstrap(self.bootstrap_path)["google_drive"]["memory_manifest_file_id"], identifier)

    def assert_ambiguous_seed_blocks_rerun(self, before):
        self.assertEqual(self.bootstrap_path.read_bytes(), before)
        created = list(self.drive.memory_manifests)
        self.assertEqual(len(created), 1)
        uploads = self.drive.uploads
        with self.assertRaises(ProvisioningRecoveryRequired) as raised:
            self.run_provision()
        self.assertNotIn(PRIVATE, str(raised.exception))
        self.assertEqual(self.drive.memory_manifests, created)
        self.assertEqual(self.drive.uploads, uploads)
        self.assertEqual(self.bootstrap_path.read_bytes(), before)

    def test_lost_manifest_upload_response_blocks_rerun_without_second_authority(self):
        before = self.bootstrap_path.read_bytes()
        self.drive.fail_memory = "manifest-response"
        with self.assertRaises(ProvisioningError):
            self.run_provision()
        self.drive.fail_memory = None
        self.assert_ambiguous_seed_blocks_rerun(before)

    def test_receipt_id_write_failure_blocks_rerun_without_second_authority(self):
        before = self.bootstrap_path.read_bytes()
        with patch("config.cloud_provisioning._Receipt.write", side_effect=OSError(PRIVATE)):
            with self.assertRaises(ProvisioningError):
                self.run_provision()
        self.assert_ambiguous_seed_blocks_rerun(before)

    def test_bootstrap_failure_reports_safe_recovery_and_receipt_rerun_reuses_seed(self):
        before = (self.local_files(), self.bootstrap_path.read_bytes())
        with patch("config.cloud_provisioning.save_bootstrap", side_effect=OSError(PRIVATE)):
            with self.assertRaises(BootstrapPublicationError) as raised:
                self.run_provision()
        self.assertNotIn(PRIVATE, str(raised.exception))
        self.assertEqual((self.local_files(), self.bootstrap_path.read_bytes()), before)
        self.assertEqual(self.drive.manifest["revision"], 5)
        created = list(self.drive.memory_manifests)
        result = self.run_provision()
        self.assertTrue(result.memory_reused)
        self.assertEqual(self.drive.memory_manifests, created)
        receipt = self.receipt_path.read_bytes()
        self.assertNotIn(b"robot-a", receipt)
        self.assertNotIn(b"private-test", receipt)

    def test_receipt_existing_invalid_authority_cannot_be_silently_replaced(self):
        with patch("config.cloud_provisioning.save_bootstrap", side_effect=OSError(PRIVATE)):
            with self.assertRaises(BootstrapPublicationError):
                self.run_provision()
        identifier = self.drive.memory_manifests[0]
        del self.drive.files[identifier]
        before = self.drive.uploads
        with self.assertRaises(ProvisioningError):
            self.run_provision()
        self.assertEqual(self.drive.memory_manifests, [identifier])
        self.assertEqual(self.drive.uploads, before)

    def test_applied_runtime_source_ignores_save_without_apply(self):
        self.local.prepare_runtime()
        changed = copy.deepcopy(self.fixture.overrides)
        changed["server"] = {"port": 8022}
        changed["Memory"] = {"ExplicitAlias": {"path": str(self.directory / "not-applied.yaml")}}
        with self.local.locked():
            self.local.commit_unlocked(changed)
        before = self.local_files()
        self.run_provision()
        self.assert_local_unchanged(before)
        self.assertEqual(self.effective()["server"]["port"], 8000)
        self.assertEqual(self.effective()["Memory"]["ExplicitAlias"]["path"], str(self.memory_path))

    def test_nonexplicit_memory_needs_no_memory_metadata_or_stale_yaml(self):
        changed = copy.deepcopy(self.fixture.overrides)
        changed["selected_module"] = {"Memory": "Disabled"}
        changed["Memory"] = {"Disabled": {"type": "nomem"}}
        with self.local.locked():
            self.local.commit_unlocked(changed)
        self.memory_path.write_text("invalid stale memory: [")
        before = self.local_files()
        result = self.run_provision()
        self.assertIsNone(result.memory_revision)
        self.assertEqual(self.drive.memory_manifests, [])
        self.assertNotIn("memory_manifest_file_id", load_bootstrap(self.bootstrap_path)["google_drive"])
        self.assert_local_unchanged(before)

    def test_invalid_reused_sound_pointer_fails_before_upload(self):
        self.run_provision()
        pointer = self.effective()["static_soundbank"]["entries"]["Hello"]["cloud"]
        self.drive.files[pointer["file_id"]] = b"corrupt remote blob"
        before = self.drive.uploads, self.bootstrap_path.read_bytes(), copy.deepcopy(self.drive.manifest)
        with self.assertRaises(ProvisioningError):
            self.run_provision()
        self.assertEqual((self.drive.uploads, self.bootstrap_path.read_bytes(), self.drive.manifest), before)

    def test_secret_persistence_failure_prevents_any_upload(self):
        changed = copy.deepcopy(self.fixture.overrides)
        changed["LLM"]["Test"]["api_key"] = "new-private-secret"
        with self.local.locked():
            self.local.commit_unlocked(changed)
        with patch.object(self.fixture.secrets, "put_many", side_effect=OSError(PRIVATE)):
            with self.assertRaises(ProvisioningError):
                self.run_provision()
        self.assertEqual(self.drive.uploads, 0)

    def test_final_verification_rejects_local_memory_changes_before_bootstrap(self):
        before = self.bootstrap_path.read_bytes()
        self.drive.after_config = lambda: self.memory_path.write_text(yaml.safe_dump({"robot-a": [entry(content="Changed live fact")]}))
        with self.assertRaises(ProvisioningConflict):
            self.run_provision()
        self.assertEqual(self.bootstrap_path.read_bytes(), before)


class ProvisioningTransportAndCLITests(unittest.TestCase):
    def test_folder_preflight_checks_capability_without_write_or_scope_expansion(self):
        valid = {"id": "folder", "mimeType": "application/vnd.google-apps.folder",
                 "capabilities": {"canAddChildren": True}}
        session = Mock()
        session.request.return_value = Mock(status_code=200, json=Mock(return_value=valid))
        transport = GoogleDriveTransport("private-creds.json", session=session)
        transport.validate_folder("folder")
        self.assertEqual(session.request.call_args.args[0], "GET")
        self.assertEqual(transport.SCOPES, ["https://www.googleapis.com/auth/drive.file"])
        session.request.return_value.json.return_value = dict(valid, capabilities={"canAddChildren": False})
        with self.assertRaises(OSError):
            transport.validate_folder("folder")

    def cli(self):
        path = Path(__file__).resolve().parents[3] / "scripts/provision_cloud_state.py"
        spec = importlib.util.spec_from_file_location("provision_cli", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_cli_only_reports_safe_status_and_sanitizes_provider_errors(self):
        from config.cloud_provisioning import ProvisioningResult
        cli = self.cli()
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(cli, "load_bootstrap", return_value={"config_provider": "local", "node_id": "test-node"}), \
                redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(cli.main(["--from-local"], provisioner=Mock(return_value=ProvisioningResult(5, 1, False))), 0)
            self.assertEqual(cli.main(["--from-local"], provisioner=Mock(side_effect=OSError(PRIVATE))), 4)
        text = output.getvalue() + errors.getvalue()
        self.assertIn("Provider: still local", text)
        self.assertIn("Ready to switch: yes", text)
        for value in PRIVATE.split():
            self.assertNotIn(value, text)

    def test_cli_parse_errors_do_not_echo_private_values(self):
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.assertEqual(self.cli().main(["--unknown", PRIVATE]), 4)
        self.assertNotIn(PRIVATE, errors.getvalue())
