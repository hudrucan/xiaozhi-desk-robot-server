"""Cloud binary transactions, offline materialization and unchanged local playback."""

import copy
import io
import json
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import yaml

from config.cloud_layers import initial_cloud_object, resolve_layers, validate_layers
from config.cloud_secrets import LocalSecretStore
from config.config_loader import merge_configs
from config.config_store import ConfigConflict, ConfigUnavailable, checksum, create_config_store
from config.config_validation import validate_config
from config.drive_transport import GoogleDriveTransport
from core.soundbank import SoundbankAuthoringService
from core.soundbank_cleanup import SoundbankCleanup
from core.utils import p3
from core.utils.config_editor import ConfigEditor
import test_cloud_config as config_tests

MemoryDrive = config_tests.MemoryDrive


def wav_bytes(value=0):
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24000)
        stream.writeframes(int(value).to_bytes(2, "little", signed=True) * 1440)
    return output.getvalue()


def p3_bytes(value=0):
    import opuslib_next
    encoder = opuslib_next.Encoder(24000, 1, opuslib_next.APPLICATION_AUDIO)
    packet = encoder.encode(int(value).to_bytes(2, "little", signed=True) * 1440, 1440)
    return p3.encode_opus_packets([packet])


class AssetDrive(MemoryDrive):
    def __init__(self, defaults, overrides):
        self.events = []
        self.blobs = []
        self.fail_blob = False
        self.corrupt_blob = False
        super().__init__(defaults, overrides)

    def download(self, file_id):
        self.events.append(("download", file_id))
        return super().download(file_id)

    def upload_blob(self, folder_id, content, name, mime_type):
        self.events.append(("blob", name))
        if self.fail_blob:
            raise OSError("private credential and filesystem path")
        file_id = MemoryDrive.upload_immutable(self, folder_id, content, name)
        self.blobs.append((file_id, name, mime_type))
        if self.corrupt_blob:
            self.files[file_id] = b"corrupt blob"
        return file_id

    def upload_immutable(self, folder_id, content, name):
        self.events.append(("config", name))
        return super().upload_immutable(folder_id, content, name)

    def replace_manifest(self, file_id, content, etag):
        self.events.append(("cas", file_id))
        return super().replace_manifest(file_id, content, etag)


class CloudSoundbankTests(unittest.TestCase):
    def setUp(self):
        self.fixture = config_tests.CloudConfigTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = (self.fixture.directory / "soundbank").resolve()
        self.canonical = wav_bytes()
        self.optimized = p3_bytes()
        (self.root / "hello.wav").write_bytes(self.canonical)
        (self.root / "hello.p3").write_bytes(self.optimized)
        self.entry = {"file": "hello.wav", "text": "Hello robot", "generated_by": {"provider": "Test"},
                      "optimized": {"file": "hello.p3", "codec": "opus", "sample_rate": 24000,
                                    "channels": 1, "frame_duration_ms": 60}}
        self.fixture.defaults["xiaozhi"] = {"audio_params": {"sample_rate": 24000}}
        self.fixture.defaults["static_soundbank"]["enabled"] = True
        self.fixture.default_path.write_text(yaml.safe_dump(self.fixture.defaults))
        overrides = copy.deepcopy(self.fixture.cloud_overrides)
        overrides["static_soundbank"]["entries"] = {"Hello": copy.deepcopy(self.entry)}
        self.drive = AssetDrive(self.fixture.defaults, overrides)
        self.obj = initial_cloud_object(overrides, "test-node")
        self.drive.publish_object(self.obj, 1)
        self.fixture.drive = self.drive
        self.store = self.fixture.new_cloud()
        self.fixture.store = self.store
        self.fixture.boot()
        self.editor = ConfigEditor(self.store)
        self.cleanup = SoundbankCleanup(self.store.prepare_runtime())
        self.cleanup.journal = self.fixture.directory / "cleanup.json"
        self.drive.events.clear()

    def save(self, patch=None, cleanup=None, revision=1):
        return self.editor.update(patch if patch is not None else {"prompt": "Cloud save"},
                                  soundbank_cleanup=cleanup, base_revision=revision)

    def current_object(self):
        return json.loads(self.drive.files[self.drive.manifest["config"]["file_id"]])

    def current_entry(self):
        return self.current_object()["layers"]["nodes"]["test-node"]["overrides"]["static_soundbank"]["entries"]["Hello"]

    def asset_pointer(self, content, file_id):
        return {"file_id": file_id, "sha256": checksum(content), "size": len(content)}

    def cached_asset(self, content, suffix=".wav"):
        return self.store.soundbank_assets.cache_dir / (checksum(content) + suffix)

    def boot_pointer_revision_one(self):
        self.publish_pointer_config(revision=1)
        # Start this lifecycle regression with a fresh revision-1 process/cache.
        for name in ("desired.json", "active.json"):
            (self.store.cache_dir / name).unlink(missing_ok=True)
        self.store = self.fixture.new_cloud()
        self.fixture.store = self.store
        self.fixture.boot()
        self.editor = ConfigEditor(self.store)
        return self.store.prepare_runtime()

    def publish_new_asset_revision(self, *, different_names=False):
        obj = self.current_object()
        entry = obj["layers"]["nodes"]["test-node"]["overrides"]["static_soundbank"]["entries"]["Hello"]
        changed = wav_bytes(500)
        self.drive.files["wav-new"] = changed
        entry["cloud"] = self.asset_pointer(changed, "wav-new")
        if different_names:
            entry["file"] = "new.wav"
            entry["optimized"]["file"] = "new.p3"
        self.drive.publish_object(obj, 2)
        return changed

    def two_node_soundbank(self, override, *, pointers=True):
        obj = copy.deepcopy(self.obj)
        obj["layers"]["nodes"]["test-node"]["overrides"].pop("static_soundbank")
        entry = copy.deepcopy(self.entry)
        entry["file"] = "global.wav"
        entry["optimized"]["file"] = "global.p3"
        (self.root / "global.wav").write_bytes(self.canonical)
        (self.root / "global.p3").write_bytes(self.optimized)
        self.node2_wav = wav_bytes(500)
        self.node2_p3 = p3_bytes(500)
        (self.root / "node2.wav").write_bytes(self.node2_wav)
        (self.root / "node2.p3").write_bytes(self.node2_p3)
        if pointers:
            self.drive.files["global-wav"] = self.canonical
            self.drive.files["global-p3"] = self.optimized
            entry["cloud"] = self.asset_pointer(self.canonical, "global-wav")
            entry["optimized"]["cloud"] = self.asset_pointer(self.optimized, "global-p3")
        obj["layers"]["global"] = {"static_soundbank": {"entries": {"Hello": entry}}}
        obj["layers"]["nodes"]["node2"] = {
            "environment": None, "role": None,
            "overrides": {"static_soundbank": {"entries": {"Hello": copy.deepcopy(override)}}},
        }
        return obj

    def resolved_entry(self, obj, node_id):
        defaults, overrides = resolve_layers(obj, node_id, self.fixture.defaults)
        return merge_configs(defaults, overrides)["static_soundbank"]["entries"]["Hello"]

    def node2_store(self):
        return create_config_store(
            {**self.fixture.bootstrap, "node_id": "node2"}, transport=self.drive,
            cache_dir=self.fixture.directory / "node2-cache", default_path=str(self.fixture.default_path),
            secret_provider=LocalSecretStore("node2", self.fixture.directory / "node2-secrets"),
        )

    def publish_pointer_config(self, *, canonical=None, optimized=None, revision=2):
        entry = copy.deepcopy(self.entry)
        canonical = self.canonical if canonical is None else canonical
        optimized = self.optimized if optimized is None else optimized
        self.drive.files["wav-asset"] = canonical
        self.drive.files["p3-asset"] = optimized
        entry["cloud"] = self.asset_pointer(canonical, "wav-asset")
        entry["optimized"]["cloud"] = self.asset_pointer(optimized, "p3-asset")
        obj = copy.deepcopy(self.obj)
        obj["layers"]["nodes"]["test-node"]["overrides"]["static_soundbank"]["entries"]["Hello"] = entry
        self.drive.publish_object(obj, revision)
        return entry

    def assert_old_publication(self, original, cleanup):
        self.assertEqual(self.drive.manifest, original)
        self.assertEqual(self.fixture.status()["desired_revision"], 1)
        self.assertEqual(self.fixture.status()["active_revision"], 1)
        cleanup.after_save.assert_not_called()

    def test_save_cloudifies_retained_wav_and_p3_before_config_cas(self):
        cleanup = Mock()
        cleanup.lock = self.cleanup.lock
        self.save(cleanup=cleanup)
        entry = self.current_entry()
        for metadata, content in ((entry, self.canonical), (entry["optimized"], self.optimized)):
            pointer = metadata["cloud"]
            self.assertEqual(pointer["sha256"], checksum(content))
            self.assertEqual(pointer["size"], len(content))
            self.assertEqual(self.drive.files[pointer["file_id"]], content)
            self.assertEqual(self.cached_asset(content, Path(metadata["file"]).suffix).read_bytes(), content)
        self.assertEqual([blob[2] for blob in self.drive.blobs], ["audio/wav", "application/octet-stream"])
        for _, name, _ in self.drive.blobs:
            self.assertIn("soundbank-", name)
        config_index = next(i for i, item in enumerate(self.drive.events) if item[0] == "config")
        cas_index = next(i for i, item in enumerate(self.drive.events) if item[0] == "cas")
        for file_id, _, _ in self.drive.blobs:
            verified_index = self.drive.events.index(("download", file_id))
            self.assertLess(verified_index, config_index)
        self.assertLess(config_index, cas_index)
        self.assertEqual(cas_index, len(self.drive.events) - 1)
        # Ordinary retained-asset cloudification does not invent a cleanup action.
        cleanup.after_save.assert_not_called()
        self.assertEqual(self.fixture.status()["active_revision"], 1)

    def test_soundbank_edit_is_cloudified_before_cleanup_prepare(self):
        cleanup = Mock()
        cleanup.lock = self.cleanup.lock

        def prepare(previous, candidate, draft_id, retired):
            metadata = candidate["static_soundbank"]["entries"]["Hello"]
            self.assertIn("cloud", metadata)
            self.assertIn("cloud", metadata["optimized"])
            self.assertEqual(len(self.drive.blobs), 2)
            self.assertEqual(self.drive.manifest["revision"], 1)

        cleanup.prepare_save.side_effect = prepare
        self.save({"static_soundbank": {"entries": {"Hello": self.entry}}}, cleanup=cleanup)
        cleanup.prepare_save.assert_called_once()
        cleanup.after_save.assert_called_once()

    def test_matching_current_pointers_reused_even_if_ui_drops_metadata(self):
        self.save()
        self.drive.events.clear()
        result = self.save({"static_soundbank": {"entries": {"Hello": self.entry}}}, revision=2)
        self.assertEqual(len(self.drive.blobs), 2)
        self.assertEqual(result["configuration_source"]["desired_revision"], 3)
        self.assertFalse(any(event[0] == "blob" for event in self.drive.events))

    def test_transaction_deduplicates_same_content(self):
        duplicate = copy.deepcopy(self.entry)
        duplicate["file"] = "copy.wav"
        duplicate["optimized"]["file"] = "copy.p3"
        (self.root / "copy.wav").write_bytes(self.canonical)
        (self.root / "copy.p3").write_bytes(self.optimized)
        self.save({"static_soundbank": {"entries": {"Hello": self.entry, "Other": duplicate}}})
        entries = self.current_object()["layers"]["nodes"]["test-node"]["overrides"]["static_soundbank"]["entries"]
        self.assertEqual(len(self.drive.blobs), 2)
        self.assertEqual(entries["Hello"]["cloud"], entries["Other"]["cloud"])
        self.assertEqual(entries["Hello"]["optimized"]["cloud"], entries["Other"]["optimized"]["cloud"])

    def test_same_filename_changed_content_gets_new_immutable_pointer(self):
        self.save()
        old_entry = self.current_entry()
        changed = wav_bytes(500)
        (self.root / "hello.wav").write_bytes(changed)
        self.save(revision=2)
        new_entry = self.current_entry()
        self.assertNotEqual(old_entry["cloud"]["file_id"], new_entry["cloud"]["file_id"])
        self.assertEqual(new_entry["cloud"]["sha256"], checksum(changed))
        self.assertEqual(new_entry["optimized"]["cloud"], old_entry["optimized"]["cloud"])
        self.assertEqual(len(self.drive.blobs), 3)
        self.assertEqual(self.drive.files[old_entry["cloud"]["file_id"]], self.canonical)
        self.assertEqual(self.cached_asset(self.canonical).read_bytes(), self.canonical)
        self.assertEqual(self.cached_asset(changed).read_bytes(), changed)

    def test_missing_retained_asset_rejects_save_without_publication(self):
        original = copy.deepcopy(self.drive.manifest)
        cleanup = Mock()
        cleanup.lock = self.cleanup.lock
        (self.root / "hello.wav").unlink()
        with self.assertRaises(ConfigUnavailable):
            self.save(cleanup=cleanup)
        self.assert_old_publication(original, cleanup)
        self.assertEqual(self.drive.uploads, 0)

    def test_invalid_local_p3_rejected_before_any_binary_upload(self):
        for content in (b"invalid p3", p3.encode_opus_packets([b"not opus"])):
            (self.root / "hello.p3").write_bytes(content)
            with self.subTest(content=content), self.assertRaises(ConfigUnavailable):
                self.save()
            self.assertEqual(self.drive.manifest["revision"], 1)
            self.assertEqual(self.drive.uploads, 0)

    def test_stereo_or_wrong_duration_p3_rejected_before_upload(self):
        import opuslib_next
        from opuslib_next.api.decoder import packet_get_nb_channels

        encoder = opuslib_next.Encoder(24000, 1, opuslib_next.APPLICATION_AUDIO)
        packet = encoder.encode(b"\0" * 2880, 1440)
        stereo_packet = bytes([packet[0] | 4]) + packet[1:]
        self.assertEqual(packet_get_nb_channels(stereo_packet), 2)
        short_packet = encoder.encode(b"\0" * 960, 480)
        for packet in (stereo_packet, short_packet):
            (self.root / "hello.p3").write_bytes(p3.encode_opus_packets([packet]))
            with self.subTest(packet=packet), self.assertRaises(ConfigUnavailable):
                self.save()
            self.assertEqual(self.drive.uploads, 0)
            self.assertEqual(self.drive.manifest["revision"], 1)

    def test_binary_failure_or_bad_verification_aborts_and_skips_cleanup(self):
        original = copy.deepcopy(self.drive.manifest)
        desired = (self.store.cache_dir / "desired.json").read_bytes()
        active = (self.store.cache_dir / "active.json").read_bytes()
        for flag in ("fail_blob", "corrupt_blob"):
            cleanup = Mock()
            cleanup.lock = self.cleanup.lock
            setattr(self.drive, flag, True)
            with self.subTest(flag=flag), self.assertRaises(ConfigUnavailable) as raised:
                self.save({"static_soundbank": {"entries": {"Hello": self.entry}}}, cleanup=cleanup)
            setattr(self.drive, flag, False)
            self.assert_old_publication(original, cleanup)
            cleanup.prepare_save.assert_not_called()
            self.assertEqual((self.store.cache_dir / "desired.json").read_bytes(), desired)
            self.assertEqual((self.store.cache_dir / "active.json").read_bytes(), active)
            self.assertNotIn("private credential", str(raised.exception))
            self.assertIsNone(raised.exception.__cause__)
            self.assertTrue(raised.exception.__suppress_context__)

    def test_final_cas_conflict_keeps_winner_and_leaves_orphan_assets(self):
        winner = copy.deepcopy(self.obj)
        winner["layers"]["nodes"]["test-node"]["overrides"]["prompt"] = "Winning writer"
        self.drive.before_commit = lambda: self.drive.publish_object(winner, 2)
        cleanup = Mock()
        cleanup.lock = self.cleanup.lock
        active = (self.store.cache_dir / "active.json").read_bytes()
        with self.assertRaises(ConfigConflict):
            self.save({"static_soundbank": {"entries": {"Hello": self.entry}}}, cleanup=cleanup)
        self.assertEqual(self.current_object(), winner)
        self.assertEqual(len(self.drive.blobs), 2)
        self.assertEqual(self.fixture.status()["active_revision"], 1)
        self.assertEqual((self.store.cache_dir / "active.json").read_bytes(), active)
        cleanup.after_save.assert_not_called()

    def test_startup_materializes_missing_pair_before_runtime_selection(self):
        self.publish_pointer_config()
        (self.root / "hello.wav").unlink()
        (self.root / "hello.p3").unlink()
        active = (self.store.cache_dir / "active.json").read_bytes()
        restarted = self.fixture.new_cloud()
        runtime = restarted.prepare_runtime()
        self.assertEqual((self.root / "hello.wav").read_bytes(), self.canonical)
        self.assertEqual((self.root / "hello.p3").read_bytes(), self.optimized)
        self.assertEqual((self.store.cache_dir / "active.json").read_bytes(), active)
        self.assertEqual(runtime["static_soundbank"]["entries"]["Hello"]["cloud"]["file_id"], "wav-asset")
        self.assertFalse(list(self.root.glob(".cloud-soundbank-*")))
        restarted.mark_applied()
        self.assertEqual(self.fixture.status(restarted)["active_revision"], 2)

    def test_valid_local_assets_need_no_asset_download(self):
        self.publish_pointer_config()
        self.drive.events.clear()
        self.fixture.new_cloud().prepare_runtime()
        self.assertNotIn(("download", "wav-asset"), self.drive.events)
        self.assertNotIn(("download", "p3-asset"), self.drive.events)

    def test_corrupt_local_assets_are_replaced_after_verified_download(self):
        self.publish_pointer_config()
        (self.root / "hello.wav").write_bytes(b"corrupt local")
        (self.root / "hello.p3").write_bytes(b"corrupt local p3")
        self.fixture.new_cloud().prepare_runtime()
        self.assertEqual((self.root / "hello.wav").read_bytes(), self.canonical)
        self.assertEqual((self.root / "hello.p3").read_bytes(), self.optimized)
        self.assertIn(("download", "wav-asset"), self.drive.events)
        self.assertIn(("download", "p3-asset"), self.drive.events)

    def test_corrupt_downloaded_p3_is_rejected_before_apply_or_any_replace(self):
        self.publish_pointer_config(optimized=b"hash-correct but invalid p3")
        (self.root / "hello.wav").write_bytes(b"old local wav")
        active = (self.store.cache_dir / "active.json").read_bytes()
        restarted = self.fixture.new_cloud()
        with self.assertRaises(ConfigUnavailable):
            restarted.prepare_runtime()
        self.assertIsNone(restarted.runtime_snapshot)
        with self.assertRaises(ValueError):
            restarted.mark_applied()
        self.assertEqual((self.root / "hello.wav").read_bytes(), b"old local wav")
        self.assertEqual((self.root / "hello.p3").read_bytes(), self.optimized)
        self.assertEqual((self.store.cache_dir / "active.json").read_bytes(), active)
        self.assertFalse(list(self.root.glob(".cloud-soundbank-*")))
        self.assertFalse(self.cached_asset(b"hash-correct but invalid p3", ".p3").exists())
        self.assertFalse(list(self.store.soundbank_assets.cache_dir.glob(".cloud-soundbank-*")))

    def test_bad_download_hash_or_size_never_replaces_local_file(self):
        self.publish_pointer_config()
        (self.root / "hello.wav").write_bytes(b"old local wav")
        for remote in (self.canonical + b"extra", wav_bytes(500)):
            self.drive.files["wav-asset"] = remote
            restarted = self.fixture.new_cloud()
            with self.subTest(remote=remote), self.assertRaises(ConfigUnavailable):
                restarted.prepare_runtime()
            self.assertEqual((self.root / "hello.wav").read_bytes(), b"old local wav")
            self.assertIsNone(restarted.runtime_snapshot)
            self.assertFalse(list(self.root.glob(".cloud-soundbank-*")))

    def test_atomic_replace_failure_is_sanitized_and_does_not_apply(self):
        self.publish_pointer_config()
        (self.root / "hello.wav").write_bytes(b"old local wav")
        active = (self.store.cache_dir / "active.json").read_bytes()
        restarted = self.fixture.new_cloud()
        with patch("config.cloud_soundbank.os.replace", side_effect=OSError("private path /node/assets")):
            with self.assertRaises(ConfigUnavailable) as raised:
                restarted.prepare_runtime()
        self.assertNotIn("/node/assets", str(raised.exception))
        self.assertIsNone(restarted.runtime_snapshot)
        with self.assertRaises(ValueError):
            restarted.mark_applied()
        self.assertEqual((self.root / "hello.wav").read_bytes(), b"old local wav")
        self.assertEqual((self.store.cache_dir / "active.json").read_bytes(), active)
        self.assertFalse(list(self.root.glob(".cloud-soundbank-*")))

    def test_nested_assets_are_materialized_in_the_configured_root(self):
        entry = self.publish_pointer_config()
        entry["file"] = "nested/hello.wav"
        entry["optimized"]["file"] = "nested/hello.p3"
        obj = self.current_object()
        obj["layers"]["nodes"]["test-node"]["overrides"]["static_soundbank"]["entries"]["Hello"] = entry
        self.drive.publish_object(obj, 3)
        self.fixture.new_cloud().prepare_runtime()
        self.assertEqual((self.root / "nested/hello.wav").read_bytes(), self.canonical)
        self.assertEqual((self.root / "nested/hello.p3").read_bytes(), self.optimized)

    def test_offline_active_lkg_uses_verified_assets_without_asset_fetch(self):
        self.publish_pointer_config()
        applied = self.fixture.boot(self.fixture.new_cloud())
        self.drive.offline = True
        with patch.object(self.drive, "download", side_effect=AssertionError("Offline boot attempted asset download")):
            restarted = self.fixture.new_cloud()
            restarted.prepare_runtime()
            restarted.mark_applied()
        self.assertEqual(self.fixture.status(restarted)["runtime_source"], "active_lkg")
        self.assertEqual(applied.active_revision, restarted.active_revision)

    def test_offline_missing_or_corrupt_active_asset_cannot_be_applied(self):
        self.publish_pointer_config()
        self.fixture.boot(self.fixture.new_cloud())
        active = (self.store.cache_dir / "active.json").read_bytes()
        self.cached_asset(self.canonical).unlink()
        self.drive.offline = True
        for corrupt in (False, True):
            if corrupt:
                (self.root / "hello.wav").write_bytes(b"corrupt offline")
            else:
                (self.root / "hello.wav").unlink()
            restarted = self.fixture.new_cloud()
            with self.subTest(corrupt=corrupt), self.assertRaises(ConfigUnavailable):
                restarted.prepare_runtime()
            self.assertIsNone(restarted.runtime_snapshot)
            with self.assertRaises(ValueError):
                restarted.mark_applied()
            self.assertEqual((self.store.cache_dir / "active.json").read_bytes(), active)

    def test_failed_desired_startup_restores_exact_active_bytes_offline(self):
        self.boot_pointer_revision_one()
        active = (self.store.cache_dir / "active.json").read_bytes()
        # Simulate an active snapshot from before the content cache was introduced.
        self.cached_asset(self.canonical).unlink()
        changed = self.publish_new_asset_revision()
        candidate = self.fixture.new_cloud()
        candidate.prepare_runtime()
        self.assertEqual((self.root / "hello.wav").read_bytes(), changed)
        self.assertEqual(self.cached_asset(self.canonical).read_bytes(), self.canonical)
        self.assertEqual(self.cached_asset(changed).read_bytes(), changed)
        self.assertEqual((self.store.cache_dir / "active.json").read_bytes(), active)
        self.assertEqual(self.fixture.status(candidate)["runtime_revision"], 2)
        # Listener startup fails: deliberately do not call candidate.mark_applied().
        self.drive.offline = True
        with patch.object(self.drive, "download", side_effect=AssertionError("LKG attempted Drive asset fetch")):
            restarted = self.fixture.new_cloud()
            restarted.prepare_runtime()
            self.assertEqual(self.fixture.status(restarted)["runtime_source"], "active_lkg")
            self.assertEqual(self.fixture.status(restarted)["runtime_revision"], 1)
            restarted.mark_applied()
        self.assertEqual(restarted.active_revision, 1)
        self.assertEqual((self.root / "hello.wav").read_bytes(), self.canonical)
        self.assertEqual((self.store.cache_dir / "active.json").read_bytes(), active)

    def test_startup_cleanup_retirement_cannot_destroy_active_offline_recovery(self):
        previous = self.boot_pointer_revision_one()
        active = (self.store.cache_dir / "active.json").read_bytes()
        self.cached_asset(self.canonical).unlink()
        self.cached_asset(self.optimized, ".p3").unlink()
        self.publish_new_asset_revision(different_names=True)
        candidate = self.fixture.new_cloud()
        runtime = candidate.prepare_runtime()
        cleanup = SoundbankCleanup(previous)
        cleanup.journal = self.fixture.directory / "startup-cleanup.json"
        cleanup.prepare_save(previous, runtime)
        startup_cleanup = SoundbankCleanup(runtime)
        startup_cleanup.journal = cleanup.journal
        # SettingsHandler runs this exact path before listeners/mark_applied().
        result = ConfigEditor(candidate).cleanup_soundbank(startup_cleanup)
        self.assertEqual(result["deleted"], 2)
        self.assertFalse((self.root / "hello.wav").exists())
        self.assertFalse((self.root / "hello.p3").exists())
        self.assertEqual(self.cached_asset(self.canonical).read_bytes(), self.canonical)
        self.assertEqual(self.cached_asset(self.optimized, ".p3").read_bytes(), self.optimized)
        self.assertEqual((self.store.cache_dir / "active.json").read_bytes(), active)
        self.drive.offline = True
        with patch.object(self.drive, "download", side_effect=AssertionError("Cleanup lost active asset cache")):
            restarted = self.fixture.new_cloud()
            restarted.prepare_runtime()
            restarted.mark_applied()
        self.assertEqual(restarted.active_revision, 1)
        self.assertEqual((self.root / "hello.wav").read_bytes(), self.canonical)
        self.assertEqual((self.root / "hello.p3").read_bytes(), self.optimized)

    def test_unrecoverable_active_bytes_abort_before_desired_overwrites_target(self):
        self.boot_pointer_revision_one()
        self.cached_asset(self.canonical).unlink()
        (self.root / "hello.wav").write_bytes(b"unrecoverable active bytes")
        self.publish_new_asset_revision()
        download = self.drive.download

        def unavailable_active(file_id):
            if file_id == "wav-asset":
                raise ConfigUnavailable("private provider detail /node/assets")
            return download(file_id)

        with patch.object(self.drive, "download", side_effect=unavailable_active):
            candidate = self.fixture.new_cloud()
            with self.assertRaises(ConfigUnavailable) as raised:
                candidate.prepare_runtime()
        self.assertNotIn("/node/assets", str(raised.exception))
        self.assertIsNone(candidate.runtime_snapshot)
        self.assertEqual((self.root / "hello.wav").read_bytes(), b"unrecoverable active bytes")
        self.assertEqual(candidate.active_revision, 1)

    def test_cache_corruption_is_rejected_offline_and_repaired_only_from_verified_bytes(self):
        self.boot_pointer_revision_one()
        cached = self.cached_asset(self.canonical)
        cached.write_bytes(b"corrupt cached bytes")
        (self.root / "hello.wav").unlink()
        active = (self.store.cache_dir / "active.json").read_bytes()
        self.drive.offline = True
        restarted = self.fixture.new_cloud()
        with self.assertRaises(ConfigUnavailable):
            restarted.prepare_runtime()
        self.assertIsNone(restarted.runtime_snapshot)
        with self.assertRaises(ValueError):
            restarted.mark_applied()
        self.assertEqual(cached.read_bytes(), b"corrupt cached bytes")
        self.assertFalse((self.root / "hello.wav").exists())
        self.assertEqual((self.store.cache_dir / "active.json").read_bytes(), active)
        self.drive.offline = False
        self.fixture.new_cloud().prepare_runtime()
        self.assertEqual(cached.read_bytes(), self.canonical)
        self.assertEqual((self.root / "hello.wav").read_bytes(), self.canonical)

    def test_cloud_cache_cannot_be_inside_runtime_cleanup_tree_even_with_no_entries(self):
        config = self.boot_pointer_revision_one()
        config["static_soundbank"] = {"directory": str(self.fixture.directory), "entries": {}}
        with self.assertRaises(ConfigUnavailable):
            self.store.soundbank_assets.materialize(config)
        self.assertEqual(self.cached_asset(self.canonical).read_bytes(), self.canonical)

    def test_active_retention_can_download_missing_original_bytes_before_desired_startup(self):
        self.boot_pointer_revision_one()
        self.cached_asset(self.canonical).unlink()
        (self.root / "hello.wav").unlink()
        changed = self.publish_new_asset_revision()
        self.drive.events.clear()
        self.fixture.new_cloud().prepare_runtime()
        self.assertIn(("download", "wav-asset"), self.drive.events)
        self.assertEqual(self.cached_asset(self.canonical).read_bytes(), self.canonical)
        self.assertEqual((self.root / "hello.wav").read_bytes(), changed)

    def test_legacy_pointerless_entries_boot_missing_files_and_cloudify_next_save(self):
        obj = copy.deepcopy(self.obj)
        obj["layers"]["nodes"]["test-node"]["overrides"]["static_soundbank"]["entries"] = {"Hello": "hello.wav"}
        self.drive.publish_object(obj, 2)
        (self.root / "hello.wav").unlink()
        runtime = self.fixture.new_cloud().prepare_runtime()
        self.assertEqual(runtime["static_soundbank"]["entries"]["Hello"], "hello.wav")
        self.assertFalse((self.root / "hello.wav").exists())
        (self.root / "hello.wav").write_bytes(self.canonical)
        self.save(revision=2)
        self.assertEqual(self.current_entry()["file"], "hello.wav")
        self.assertIn("cloud", self.current_entry())

    def test_mp3_is_a_canonical_cloud_asset(self):
        (self.root / "sound.mp3").write_bytes(b"test mp3 payload")
        self.save({"static_soundbank": {"entries": {"Hello": "sound.mp3"}}})
        self.assertEqual(self.drive.blobs[0][2], "audio/mpeg")
        self.assertEqual(self.current_entry()["cloud"]["size"], len(b"test mp3 payload"))

    def test_canonical_p3_is_published_and_materialized_as_a_runtime_asset(self):
        self.save({"static_soundbank": {"entries": {"Hello": "hello.p3"}}})
        entry = self.current_entry()
        self.assertEqual(entry["cloud"]["sha256"], checksum(self.optimized))
        self.assertEqual(len(self.drive.blobs), 1)
        self.assertEqual(self.drive.blobs[0][2], "application/octet-stream")
        (self.root / "hello.p3").unlink()
        runtime = self.fixture.new_cloud().prepare_runtime()
        self.assertEqual((self.root / "hello.p3").read_bytes(), self.optimized)
        self.assertEqual(runtime["static_soundbank"]["entries"]["Hello"]["file"], "hello.p3")

    def assert_shared_entry_propagates(self, scope="global", legacy=False):
        obj = copy.deepcopy(self.obj)
        inherited = obj["layers"]["nodes"]["test-node"]["overrides"].pop("static_soundbank")
        if legacy:
            inherited["entries"]["Hello"] = "hello.wav"
        shared = {"static_soundbank": inherited, "prompt": "Shared prompt"}
        if scope == "global":
            obj["layers"]["global"] = shared
        else:
            obj["layers"][scope]["desk"] = shared
            obj["layers"]["nodes"]["test-node"]["role" if scope == "roles" else "environment"] = "desk"
        obj["layers"]["nodes"]["other-node"] = {"environment": None, "role": None, "overrides": {}}
        self.drive.publish_object(obj, 2)
        self.save({"log": {"log_level": "DEBUG"}}, revision=2)
        saved = self.current_object()
        self.assertEqual(saved["layers"]["nodes"]["other-node"], obj["layers"]["nodes"]["other-node"])
        overrides = saved["layers"]["nodes"]["test-node"]["overrides"]
        self.assertNotIn("prompt", overrides)
        self.assertNotIn("static_soundbank", overrides)

        def shared_layer(value):
            return value["layers"]["global"] if scope == "global" else value["layers"][scope]["desk"]

        entry = shared_layer(saved)["static_soundbank"]["entries"]["Hello"]
        self.assertIn("cloud", entry)
        without_pointers = copy.deepcopy(entry)
        without_pointers.pop("cloud")
        if "optimized" in without_pointers:
            without_pointers["optimized"].pop("cloud")
        self.assertEqual(without_pointers, {"file": "hello.wav"} if legacy else self.entry)

        def change(value):
            entry = shared_layer(value)["static_soundbank"]["entries"]["Hello"]
            entry["file"] = "updated.wav"
            entry["text"] = "Updated shared transcript"

        self.store.mutate(change, 3)
        effective = self.editor.read_public()["config"]["static_soundbank"]["entries"]["Hello"]
        self.assertEqual(effective["file"], "updated.wav")
        self.assertEqual(effective["text"], "Updated shared transcript")
        self.store.mutate(lambda value: shared_layer(value)["static_soundbank"]["entries"].pop("Hello"), 4)
        self.assertNotIn("Hello", self.editor.read_public()["config"]["static_soundbank"]["entries"])

    def test_global_entry_cloud_metadata_preserves_change_and_deletion_propagation(self):
        self.assert_shared_entry_propagates()

    def test_role_entry_cloud_metadata_preserves_change_and_deletion_propagation(self):
        self.assert_shared_entry_propagates("roles")

    def test_environment_entry_cloud_metadata_preserves_change_and_deletion_propagation(self):
        self.assert_shared_entry_propagates("environments")

    def test_inherited_legacy_string_is_normalized_in_its_shared_owner(self):
        self.assert_shared_entry_propagates(legacy=True)

    def test_composed_entry_pointers_follow_canonical_and_optimized_file_owners(self):
        obj = copy.deepcopy(self.obj)
        node = obj["layers"]["nodes"]["test-node"]
        inherited = node["overrides"].pop("static_soundbank")
        obj["layers"]["global"] = {"static_soundbank": inherited}
        node["role"] = "desk"
        obj["layers"]["roles"]["desk"] = {"static_soundbank": {"entries": {
            "Hello": {"optimized": {"file": "role.p3"}}}}}
        stale = self.asset_pointer(wav_bytes(500), "stale-pointer")
        node["overrides"]["static_soundbank"] = {"entries": {"Hello": {
            "text": "Node transcript", "cloud": stale, "optimized": {"cloud": stale}}}}
        (self.root / "role.p3").write_bytes(self.optimized)
        self.drive.publish_object(obj, 2)
        self.save({"log": {"log_level": "DEBUG"}}, revision=2)
        saved = self.current_object()["layers"]
        global_entry = saved["global"]["static_soundbank"]["entries"]["Hello"]
        role_entry = saved["roles"]["desk"]["static_soundbank"]["entries"]["Hello"]
        node_entry = saved["nodes"]["test-node"]["overrides"]["static_soundbank"]["entries"]["Hello"]
        self.assertEqual(global_entry["cloud"]["sha256"], checksum(self.canonical))
        self.assertNotIn("cloud", global_entry["optimized"])
        self.assertEqual(set(role_entry["optimized"]), {"file", "cloud"})
        self.assertEqual(role_entry["optimized"]["cloud"]["sha256"], checksum(self.optimized))
        self.assertEqual(node_entry, {"text": "Node transcript", "optimized": {}})
        effective = self.editor.read_public()["config"]["static_soundbank"]["entries"]["Hello"]
        self.assertEqual(effective["file"], "hello.wav")
        self.assertEqual(effective["optimized"]["file"], "role.p3")
        self.assertEqual(effective["text"], "Node transcript")

    def test_repo_default_asset_is_rejected_without_pinning_software_owned_values(self):
        self.fixture.defaults["static_soundbank"]["entries"] = {"Hello": self.entry}
        self.fixture.default_path.write_text(yaml.safe_dump(self.fixture.defaults))
        obj = copy.deepcopy(self.obj)
        obj["layers"]["nodes"]["test-node"]["overrides"].pop("static_soundbank")
        self.drive.publish_object(obj, 2)
        manifest = copy.deepcopy(self.drive.manifest)
        defaults = self.fixture.default_path.read_bytes()
        with self.assertRaises(ConfigUnavailable):
            self.save({"log": {"log_level": "DEBUG"}}, revision=2)
        self.assertEqual(self.drive.manifest, manifest)
        self.assertEqual(self.current_object(), obj)
        self.assertEqual(self.drive.uploads, 0)
        self.assertEqual(self.fixture.default_path.read_bytes(), defaults)

    def test_settings_prepares_sound_assets_once_then_commits_without_a_second_pass(self):
        assets = self.store.soundbank_assets
        with patch.object(self.store, "prepare_candidate_unlocked", wraps=self.store.prepare_candidate_unlocked) as prepare:
            with patch.object(assets, "publish", wraps=assets.publish) as publish:
                with patch.object(p3, "load_validated_opus_bytes", wraps=p3.load_validated_opus_bytes) as validate:
                    self.save()
        prepare.assert_called_once()
        publish.assert_called_once()
        # One local P3 inspection and one required cache-stage validation, no commit re-read.
        self.assertEqual(validate.call_count, 2)
        self.assertEqual(len(self.drive.blobs), 2)
        self.assertEqual([event[0] for event in self.drive.events].count("config"), 1)
        self.assertEqual(self.drive.events[-1][0], "cas")

    def test_direct_store_commit_prepares_and_publishes_the_same_asset_invariant(self):
        with self.store.locked():
            self.store.prepare_commit_unlocked(1)
            overrides = self.store.read_unlocked()
            with patch.object(self.store.soundbank_assets, "publish", wraps=self.store.soundbank_assets.publish) as publish:
                self.store.commit_unlocked(overrides, 1)
        publish.assert_called_once()
        self.assertIn("cloud", self.current_entry())
        self.assertIn("cloud", self.current_entry()["optimized"])
        self.assertEqual(self.cached_asset(self.canonical).read_bytes(), self.canonical)
        self.assertEqual(self.drive.events[-1][0], "cas")

    def test_every_node_canonical_pointer_follows_its_file_owner_through_read_and_lkg(self):
        obj = self.two_node_soundbank({"file": "node2.wav"})
        original = copy.deepcopy(obj)
        node1 = self.resolved_entry(obj, "test-node")
        node2 = self.resolved_entry(obj, "node2")
        self.assertEqual(node1["cloud"]["file_id"], "global-wav")
        self.assertEqual(node2["file"], "node2.wav")
        self.assertNotIn("cloud", node2)
        self.assertEqual(node2["optimized"]["cloud"]["file_id"], "global-p3")
        validated = []
        validate_layers(obj, lambda config: validated.append(copy.deepcopy(config)), self.fixture.defaults)
        self.assertNotIn("cloud", validated[1]["static_soundbank"]["entries"]["Hello"])
        self.assertEqual(obj, original)
        self.drive.publish_object(obj, 2)
        store = self.node2_store()
        self.drive.events.clear()
        runtime = store.prepare_runtime()
        self.assertNotIn("cloud", runtime["static_soundbank"]["entries"]["Hello"])
        self.assertNotIn(("download", "global-wav"), self.drive.events)
        self.assertEqual((self.root / "node2.wav").read_bytes(), self.node2_wav)
        self.assertNotIn("cloud", ConfigEditor(store).read_public()["config"]["static_soundbank"]["entries"]["Hello"])
        store.mark_applied()
        self.drive.offline = True
        with patch.object(self.drive, "download", side_effect=AssertionError("Node2 LKG fetched global canonical pointer")):
            restarted = self.node2_store()
            restarted.prepare_runtime()
            restarted.mark_applied()
        self.assertEqual(self.fixture.status(restarted)["runtime_source"], "active_lkg")
        self.assertEqual((self.root / "node2.wav").read_bytes(), self.node2_wav)

    def test_every_node_optimized_pointer_follows_its_file_owner_without_overwriting_p3(self):
        obj = self.two_node_soundbank({"optimized": {"file": "node2.p3"}})
        node1 = self.resolved_entry(obj, "test-node")
        node2 = self.resolved_entry(obj, "node2")
        self.assertEqual(node1["optimized"]["cloud"]["file_id"], "global-p3")
        self.assertEqual(node2["cloud"]["file_id"], "global-wav")
        self.assertNotIn("cloud", node2["optimized"])
        self.assertEqual(node2["optimized"]["file"], "node2.p3")
        self.drive.publish_object(obj, 2)
        self.drive.events.clear()
        store = self.node2_store()
        runtime = store.prepare_runtime()
        self.assertNotIn("cloud", runtime["static_soundbank"]["entries"]["Hello"]["optimized"])
        self.assertNotIn(("download", "global-p3"), self.drive.events)
        self.assertEqual((self.root / "node2.p3").read_bytes(), self.node2_p3)
        public = ConfigEditor(store).read_public()["config"]["static_soundbank"]["entries"]["Hello"]
        self.assertNotIn("cloud", public["optimized"])

    def test_metadata_only_node_overrides_keep_both_inherited_pointers(self):
        obj = self.two_node_soundbank({"text": "Node2 transcript", "title": "Node2 title",
                                      "generated_by": {"title": "Node2 provenance"},
                                      "optimized": {"sample_rate": 24000}})
        node1 = self.resolved_entry(obj, "test-node")
        node2 = self.resolved_entry(obj, "node2")
        self.assertEqual(node2["cloud"], node1["cloud"])
        self.assertEqual(node2["optimized"]["cloud"], node1["optimized"]["cloud"])
        self.assertEqual(node2["text"], "Node2 transcript")
        self.assertEqual(node2["generated_by"]["provider"], "Test")

    def test_two_node_saves_publish_independent_file_owner_pointers(self):
        obj = self.two_node_soundbank({"file": "node2.wav", "optimized": {"file": "node2.p3"}}, pointers=False)
        self.drive.publish_object(obj, 2)
        self.save({"log": {"log_level": "DEBUG"}}, revision=2)
        first = self.current_object()
        global_entry = first["layers"]["global"]["static_soundbank"]["entries"]["Hello"]
        self.assertIn("cloud", global_entry)
        self.assertIn("cloud", global_entry["optimized"])
        node2 = self.resolved_entry(first, "node2")
        self.assertNotIn("cloud", node2)
        self.assertNotIn("cloud", node2["optimized"])
        self.assertNotIn("static_soundbank", first["layers"]["nodes"]["test-node"]["overrides"])
        second_store = self.node2_store()
        second_store.prepare_runtime()
        second_store.mark_applied()
        ConfigEditor(second_store).update({"log": {"log_level": "DEBUG"}}, base_revision=3)
        saved = self.current_object()
        self.assertEqual(saved["layers"]["global"], first["layers"]["global"])
        owner = saved["layers"]["nodes"]["node2"]["overrides"]["static_soundbank"]["entries"]["Hello"]
        self.assertEqual(set(owner), {"file", "cloud", "optimized"})
        self.assertEqual(set(owner["optimized"]), {"file", "cloud"})
        node1 = self.resolved_entry(saved, "test-node")
        node2 = self.resolved_entry(saved, "node2")
        self.assertEqual(node1["cloud"], global_entry["cloud"])
        self.assertEqual(node1["optimized"]["cloud"], global_entry["optimized"]["cloud"])
        for metadata, content in ((node2, self.node2_wav), (node2["optimized"], self.node2_p3)):
            self.assertEqual(metadata["cloud"]["sha256"], checksum(content))
            self.assertEqual(self.drive.files[metadata["cloud"]["file_id"]], content)
        self.assertNotEqual(node1["cloud"], node2["cloud"])
        self.assertNotEqual(node1["optimized"]["cloud"], node2["optimized"]["cloud"])
        self.assertEqual(second_store.active_revision, 3)
        self.assertEqual(self.drive.events[-1][0], "cas")

    def test_environment_and_role_file_owners_invalidate_global_pointers_before_node_merge(self):
        obj = self.two_node_soundbank({"text": "Node2 metadata only"})
        layers = obj["layers"]
        layers["environments"]["production"] = {"static_soundbank": {"entries": {"Hello": {"file": "node2.wav"}}}}
        layers["roles"]["desk"] = {"static_soundbank": {"entries": {"Hello": {"optimized": {"file": "node2.p3"}}}}}
        layers["nodes"]["node2"].update(environment="production", role="desk")
        node2 = self.resolved_entry(obj, "node2")
        self.assertNotIn("cloud", node2)
        self.assertNotIn("cloud", node2["optimized"])
        self.assertEqual(node2["file"], "node2.wav")
        self.assertEqual(node2["optimized"]["file"], "node2.p3")
        self.assertIn("cloud", self.resolved_entry(obj, "test-node"))
        self.drive.publish_object(obj, 2)
        runtime = self.node2_store().prepare_runtime()
        self.assertNotIn("cloud", runtime["static_soundbank"]["entries"]["Hello"])

    def test_file_owner_own_pointers_are_preserved_and_partial_pointers_are_rejected(self):
        obj = self.two_node_soundbank({"file": "node2.wav", "cloud": self.asset_pointer(wav_bytes(500), "node2-wav"),
                                      "optimized": {"file": "node2.p3", "cloud": self.asset_pointer(p3_bytes(500), "node2-p3")}})
        node2 = self.resolved_entry(obj, "node2")
        self.assertEqual(node2["cloud"]["file_id"], "node2-wav")
        self.assertEqual(node2["optimized"]["cloud"]["file_id"], "node2-p3")
        validate_layers(obj, validate_config, self.fixture.defaults)
        for optimized in (False, True):
            invalid = copy.deepcopy(obj)
            entry = invalid["layers"]["nodes"]["node2"]["overrides"]["static_soundbank"]["entries"]["Hello"]
            (entry["optimized"] if optimized else entry)["cloud"] = {"file_id": "incomplete-pointer"}
            with self.subTest(optimized=optimized), self.assertRaises(ValueError):
                validate_layers(invalid, validate_config, self.fixture.defaults)

    def test_same_filename_explicit_owner_and_string_replacement_do_not_inherit_pointers(self):
        obj = self.two_node_soundbank({"file": "global.wav", "optimized": {"file": "global.p3"}})
        node2 = self.resolved_entry(obj, "node2")
        self.assertNotIn("cloud", node2)
        self.assertNotIn("cloud", node2["optimized"])
        obj["layers"]["nodes"]["node2"]["overrides"]["static_soundbank"]["entries"]["Hello"] = "node2.wav"
        self.assertEqual(self.resolved_entry(obj, "node2"), "node2.wav")

    def test_legacy_cloud_layouts_use_dependent_pointers_without_changing_generic_merge(self):
        central = self.two_node_soundbank({"file": "node2.wav", "optimized": {"file": "node2.p3"}})
        defaults = merge_configs(self.fixture.defaults, central["layers"]["global"])
        overrides = central["layers"]["nodes"]["node2"]["overrides"]
        ordinary = merge_configs(defaults, overrides)["static_soundbank"]["entries"]["Hello"]
        self.assertIn("cloud", ordinary)
        self.assertIn("cloud", ordinary["optimized"])
        legacy = {"schema_version": 1, "layers": {"defaults": defaults, "overrides": overrides}}
        node2 = self.resolved_entry(legacy, "node2")
        self.assertNotIn("cloud", node2)
        self.assertNotIn("cloud", node2["optimized"])
        validate_layers(legacy, validate_config, self.fixture.defaults)
        central["layers"]["global"] = {"defaults": defaults, "overrides": {}}
        node2 = self.resolved_entry(central, "node2")
        self.assertNotIn("cloud", node2)
        self.assertNotIn("cloud", node2["optimized"])

    def test_cloud_pointer_validation_rejects_malformed_fields(self):
        valid = self.asset_pointer(self.canonical, "wav-asset")
        for invalid in (None, {}, {**valid, "file_id": "bad/id"}, {**valid, "sha256": "A" * 64},
                        {**valid, "size": True}, {**valid, "size": -1}, {**valid, "size": "20"},
                        {**valid, "extra": "ignored"}):
            for optimized in (False, True):
                config = copy.deepcopy(self.cleanup.runtime_config)
                entry = config["static_soundbank"]["entries"]["Hello"]
                (entry["optimized"] if optimized else entry)["cloud"] = invalid
                with self.subTest(invalid=invalid, optimized=optimized), self.assertRaises(ValueError):
                    validate_config(config)

    def test_cloud_materialization_rejects_traversal_and_symlinks(self):
        entry = self.publish_pointer_config()
        for filename in ("../escape.wav", "/outside.wav"):
            config = copy.deepcopy(self.cleanup.runtime_config)
            config["static_soundbank"]["entries"]["Hello"] = {**entry, "file": filename}
            with self.assertRaises(ConfigUnavailable):
                self.store.soundbank_assets.materialize(config)
        self.fixture.boot(self.fixture.new_cloud())
        outside = self.fixture.directory / "outside.wav"
        outside.write_bytes(b"protected outside content")
        (self.root / "hello.wav").unlink()
        (self.root / "hello.wav").symlink_to(outside)
        with self.assertRaises(ConfigUnavailable):
            self.fixture.new_cloud().prepare_runtime()
        self.assertEqual(outside.read_bytes(), b"protected outside content")

    def test_conflicting_pointers_for_one_path_are_rejected(self):
        entry = self.publish_pointer_config()
        config = copy.deepcopy(self.cleanup.runtime_config)
        different = copy.deepcopy(entry)
        different["cloud"] = self.asset_pointer(wav_bytes(500), "different-asset")
        config["static_soundbank"]["entries"] = {"Hello": entry, "Other": different}
        with self.assertRaises(ConfigUnavailable):
            self.store.soundbank_assets.materialize(config)

    def test_cleanup_protects_cloud_metadata_references_runtime_and_drafts(self):
        self.save()
        saved = ConfigEditor(self.store).read_public()["config"]
        cleanup = self.cleanup
        # Cloud metadata is ignored by the local reference graph.
        self.assertEqual(cleanup._references(saved), {self.root / "hello.wav", self.root / "hello.p3"})
        empty = copy.deepcopy(saved)
        empty["static_soundbank"]["entries"] = {}
        cleanup.prepare_save(saved, empty)
        self.assertEqual(cleanup.after_save(empty)["deleted"], 0)
        self.assertTrue((self.root / "hello.wav").exists())
        self.assertTrue((self.root / "hello.p3").exists())
        cleanup.runtime_config = empty
        cleanup.protect_draft(self.current_entry(), "draft")
        self.assertEqual(cleanup.cleanup_pending(empty)["deleted"], 0)
        cleanup.drafts.clear()
        self.assertEqual(cleanup.cleanup_pending(empty)["deleted"], 2)
        self.assertFalse((self.root / "hello.wav").exists())
        self.assertFalse((self.root / "hello.p3").exists())
        # No delete method exists on the transport and remote objects survive.
        self.assertEqual(len(self.drive.blobs), 2)
        for file_id, _, _ in self.drive.blobs:
            self.assertIn(file_id, self.drive.files)


class SoundbankSettingsApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = CloudSoundbankTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        with patch("config.logger.setup_logging", return_value=Mock()):
            from core.api.settings_handler import SettingsHandler
        self.handler = SettingsHandler.__new__(SettingsHandler)
        self.handler.editor = self.fixture.editor
        self.handler.allow_remote = False
        self.handler.restart_required = False
        self.handler.soundbank_cleanup = Mock()
        self.handler.soundbank_cleanup.lock = self.fixture.cleanup.lock
        self.handler.soundbank = Mock()

    def request(self):
        request = Mock()
        request.content_type = "application/json"
        request.transport.get_extra_info.return_value = ("127.0.0.1", 1234)
        request.json = AsyncMock(return_value={"config": {"static_soundbank": {
            "entries": {"Hello": self.fixture.entry}}}, "base_revision": 1})
        return request

    async def test_failed_blob_upload_or_verification_returns_generic_503(self):
        fixture = self.fixture
        manifest = copy.deepcopy(fixture.drive.manifest)
        for flag in ("fail_blob", "corrupt_blob"):
            setattr(fixture.drive, flag, True)
            response = await self.handler.handle_put(self.request())
            setattr(fixture.drive, flag, False)
            self.assertEqual(response.status, 503)
            payload = json.loads(response.text)
            self.assertEqual(payload["error"],
                             "Cloud soundbank assets unavailable; configuration was not published")
            self.assertNotIn("private credential", response.text)
            self.assertEqual(fixture.drive.manifest, manifest)
            self.assertEqual(payload["configuration_source"]["desired_revision"], 1)
            self.assertEqual(payload["configuration_source"]["active_revision"], 1)
            self.handler.soundbank_cleanup.after_save.assert_not_called()

    async def test_final_asset_commit_cas_conflict_returns_409(self):
        fixture = self.fixture
        winner = copy.deepcopy(fixture.obj)
        winner["layers"]["nodes"]["test-node"]["overrides"]["prompt"] = "Winning API writer"
        fixture.drive.before_commit = lambda: fixture.drive.publish_object(winner, 2)
        response = await self.handler.handle_put(self.request())
        self.assertEqual(response.status, 409)
        self.assertTrue(json.loads(response.text)["configuration_source"]["conflict"])
        self.assertEqual(fixture.current_object(), winner)
        self.assertEqual(len(fixture.drive.blobs), 2)
        self.assertEqual(fixture.fixture.status()["active_revision"], 1)
        self.handler.soundbank_cleanup.after_save.assert_not_called()


class LocalSoundbankCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = config_tests.CloudConfigTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = (self.fixture.directory / "soundbank").resolve()
        self.config = copy.deepcopy(self.fixture.defaults)
        self.config["static_soundbank"]["enabled"] = True
        self.config["xiaozhi"] = {"audio_params": {"sample_rate": 24000}}
        self.config["selected_module"]["TTS"] = "Test"
        self.config["TTS"] = {"Test": {"voice": "test voice"}}
        self.fixture.default_path.write_text(yaml.safe_dump(self.config))
        # Fail loudly if any Local path constructs or invokes the cloud backend.
        for target in ("config.drive_transport.GoogleDriveTransport.__init__",
                       "config.drive_transport.GoogleDriveTransport._request",
                       "config.cloud_soundbank.CloudSoundbankAssets.__init__"):
            guard = patch(target, side_effect=AssertionError("Local mode touched Drive"))
            guard.start()
            self.addCleanup(guard.stop)

    def make_provider(self, sample_rate=24000, audio_format="opus"):
        with patch("config.logger.setup_logging", return_value=Mock()):
            from core.providers.tts.base import TTSProviderBase

        class TestTTS(TTSProviderBase):
            async def text_to_speak(self, text, output_file):
                raise AssertionError("Runtime fallback tried remote TTS")

        provider = TestTTS({}, False)
        provider.conn = SimpleNamespace(sample_rate=sample_rate, audio_format=audio_format)
        return provider

    def test_local_generate_optimize_save_and_cleanup_never_touch_drive(self):
        import sys
        from types import ModuleType
        bootstrap = {"config_provider": "local", "node_id": "local-node"}
        local_path = self.fixture.directory / "local-authoring/.config.yaml"
        local_path.parent.mkdir()
        local_path.write_text("{}")
        local = create_config_store(bootstrap, local_path=local_path, default_path=str(self.fixture.default_path))
        runtime = local.prepare_runtime()
        local.mark_applied()
        authoring = SoundbankAuthoringService(runtime)
        cleanup = SoundbankCleanup(runtime)
        cleanup.journal = self.fixture.directory / "local-cleanup.json"

        class Generator:
            audio_file_type = "wav"
            closed = False

            async def text_to_speak(self, text, output_file):
                return wav_bytes()

            async def close(self):
                self.closed = True

        generator = Generator()
        module = ModuleType("core.utils.modules_initialize")
        module.initialize_tts = lambda config: generator

        def normalized(source, destination, sample_rate):
            self.assertEqual(sample_rate, 24000)
            destination.write_bytes(source.read_bytes())

        # The network TTS and FFmpeg boundary are fixture-controlled; publication,
        # real libopus encoding/P3 validation, sectioned Save and cleanup run normally.
        with patch.dict(sys.modules, {"core.utils.modules_initialize": module}), \
             patch.object(authoring, "_normalize_wav", side_effect=normalized):
            generated = authoring.generate("Hello robot", "Generated hello")
            optimized = authoring.optimize(generated["file"])
        self.assertTrue(generator.closed)
        self.assertNotIn("cloud", generated)
        self.assertNotIn("cloud", generated["optimized"])
        self.assertNotIn("cloud", optimized)
        self.assertTrue((self.root / generated["file"]).exists())
        p3.load_validated_opus_file(self.root / optimized["file"], sample_rate=24000)
        generated["optimized"] = optimized
        cleanup.protect_draft(generated, "draft")
        editor = ConfigEditor(local)
        saved = editor.update({"static_soundbank": {"entries": {"Hello": generated}}},
                              soundbank_cleanup=cleanup, draft_id="draft")
        self.assertNotIn("cloud", saved["config"]["static_soundbank"]["entries"]["Hello"])
        self.assertTrue((local.local.sections_dir / "soundbank.yaml").exists())
        editor.update({"static_soundbank": {"entries": {}}}, soundbank_cleanup=cleanup, draft_id="draft")
        self.assertFalse((self.root / generated["file"]).exists())
        self.assertFalse((self.root / optimized["file"]).exists())
        self.assertFalse((self.fixture.directory / "cloud-soundbank").exists())

    def test_optimized_p3_metadata_keeps_direct_play_and_canonical_fallback(self):
        packetized = p3_bytes()
        (self.root / "hello.wav").write_bytes(wav_bytes())
        (self.root / "hello.p3").write_bytes(packetized)
        entry = {"file": "hello.wav", "text": "Spoken transcript",
                 "cloud": {"file_id": "canonical-id", "sha256": checksum(wav_bytes()), "size": len(wav_bytes())},
                 "optimized": {"file": "hello.p3", "codec": "opus", "sample_rate": 24000,
                               "channels": 1, "frame_duration_ms": 60,
                               "cloud": {"file_id": "optimized-id", "sha256": checksum(packetized), "size": len(packetized)}}}
        soundbank = {**self.config["static_soundbank"], "entries": {"Hello": entry}}
        provider = self.make_provider()
        provider._configure_static_soundbank(soundbank)
        asset = provider._resolve_static_soundbank_asset("Hello")
        self.assertTrue(asset["optimized"])
        self.assertEqual(asset["path"], self.root / "hello.p3")
        self.assertEqual(provider._prepare_static_soundbank_audio(asset),
                         tuple(p3.load_validated_opus_file(self.root / "hello.p3", sample_rate=24000)))
        # Negotiated audio mismatch retains the existing canonical fallback.
        provider = self.make_provider(audio_format="pcm")
        provider._configure_static_soundbank(soundbank)
        self.assertFalse(provider._resolve_static_soundbank_asset("Hello")["optimized"])
        self.assertEqual(provider._resolve_static_soundbank_asset("Hello")["path"], self.root / "hello.wav")
        # Malformed pointerless P3 still falls back at runtime; it is not uploaded.
        (self.root / "hello.p3").write_bytes(b"corrupt legacy p3")
        provider = self.make_provider()
        provider._configure_static_soundbank(soundbank)
        self.assertFalse(provider._resolve_static_soundbank_asset("Hello")["optimized"])
        self.assertEqual(provider._resolve_static_soundbank_asset("Hello")["path"], self.root / "hello.wav")

    def test_legacy_string_canonical_p3_preloads_and_missing_file_falls_back(self):
        (self.root / "direct.p3").write_bytes(p3_bytes())
        provider = self.make_provider()
        provider._configure_static_soundbank({**self.config["static_soundbank"],
                                             "entries": {"Direct": "direct.p3", "Missing": "missing.wav"}})
        asset = provider._resolve_static_soundbank_asset("Direct")
        self.assertTrue(asset["packets"])
        self.assertFalse(asset["optimized"])
        self.assertIsNone(provider._resolve_static_soundbank_asset("Missing"))


class BinaryTransportTests(unittest.TestCase):
    def test_binary_multipart_uses_correct_mime_and_preserves_payload(self):
        for mime in ("audio/wav", "audio/mpeg", "application/octet-stream", "application/json"):
            session = Mock()
            session.request.return_value.status_code = 200
            session.request.return_value.json.return_value = {"id": "blob-id"}
            transport = GoogleDriveTransport("unused.json", session=session)
            payload = b"\0\xff\r\nopaque binary data"
            if mime == "application/json":
                result = transport.upload_immutable("folder", payload, "config.json")
            else:
                result = transport.upload_blob("folder", payload, "sound", mime)
            self.assertEqual(result, "blob-id")
            request = session.request.call_args
            self.assertEqual(request.args[0], "POST")
            self.assertEqual(request.kwargs["params"]["uploadType"], "multipart")
            self.assertIn(('"mimeType": "' + mime + '"').encode(), request.kwargs["data"])
            self.assertIn(("Content-Type: " + mime + "\r\n\r\n").encode() + payload, request.kwargs["data"])


if __name__ == "__main__":
    unittest.main()
