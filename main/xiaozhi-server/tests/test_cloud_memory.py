"""Cloud Memory authority, transaction isolation and Local compatibility; no network."""

import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import yaml
from aiohttp import web

from config.cloud_memory import CloudMemoryStore, MemorySchema, parse_manifest
from config.config_store import ConfigConflict, ConfigUnavailable, canonical_bytes, checksum
from config.memory_reconciliation import read_local_memory
from core.memory_storage import MemoryConflict, MemoryReadOnly, MemoryUnavailable
import test_cloud_config as config_tests

# Provider and plugin base modules eagerly create a deployment logger. Unit
# fixtures must not load deployment configuration or create deployment files.
with patch("config.logger.setup_logging", return_value=Mock()):
    from core.providers.memory.mem_local_explicit.mem_local_explicit import MemoryProvider
    from core.api.memory_handler import MemoryHandler
    from plugins_func.functions.local_memory import manage_memory
    from plugins_func.register import Action


def entry(identifier="first", content="Camera uses shared I2C", **metadata):
    return MemorySchema({})._normalize_entry({
        "id": identifier, "content": content, "created_at": "2026-10-03T01:00:00+00:00",
        "updated_at": "2026-10-03T01:00:00+00:00", **metadata,
    })


class SnapshotDrive:
    def __init__(self, config_drive=None):
        self.config_drive = config_drive
        self.files = {}
        self.etag = 0
        self.events = []
        self.uploads = 0
        self.offline = False
        self.failure = None
        self.before_cas = None
        self.seed({"robot-a": [entry()], "robot-b": [entry("other", "Other device fact")]})

    def seed(self, scopes, revision=1, writer="deskbox", content=None):
        snapshot = {"schema_version": 1, "scopes": scopes}
        content = canonical_bytes(snapshot) if content is None else content
        identifier = f"memory-seed-{len(self.files)}"
        self.files[identifier] = content
        self.manifest = {"schema_version": 1, "revision": revision, "writer_node_id": writer,
                         "snapshot": {"file_id": identifier, "sha256": checksum(content)}}
        self.etag += 1

    def _available(self):
        if self.offline:
            raise ConfigUnavailable("private Drive id /private/credentials.json access-token")

    def read_manifest(self, file_id):
        self.events.append("read")
        self._available()
        if file_id != "memory-manifest":
            return self.config_drive.read_manifest(file_id)
        if self.failure == "read_race":
            raise ConfigConflict("private read race /private/credentials.json")
        return canonical_bytes(self.manifest), str(self.etag)

    def download(self, file_id):
        self.events.append("download")
        self._available()
        if file_id not in self.files:
            return self.config_drive.download(file_id)
        return self.files[file_id]

    def upload_immutable(self, folder_id, content, name):
        self.events.append("upload")
        self._available()
        if not name.startswith("memory-"):
            return self.config_drive.upload_immutable(folder_id, content, name)
        if self.failure == "upload":
            raise OSError("private Drive id /private/credentials.json access-token")
        self.uploads += 1
        identifier = f"memory-upload-{self.uploads}"
        self.files[identifier] = b"invalid" if self.failure == "verify" else content
        return identifier

    def replace_manifest(self, file_id, content, etag):
        self.events.append("cas")
        self._available()
        if file_id != "memory-manifest":
            return self.config_drive.replace_manifest(file_id, content, etag)
        if self.before_cas:
            self.before_cas()
        if self.failure == "cas" or etag != str(self.etag):
            raise ConfigConflict("private Drive id /private/credentials.json access-token")
        self.manifest = json.loads(content)
        self.etag += 1


class CloudMemoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / "configured/memory.yaml"
        self.config = {"path": str(self.path), "recall_min_score": 0.1}
        self.bootstrap = {"config_provider": "google_drive", "node_id": "deskbox",
                          "google_drive": {"folder_id": "folder",
                                           "memory_manifest_file_id": "memory-manifest"}}
        self.drive = SnapshotDrive()
        self.store = self.new_store()

    def new_store(self, node="deskbox", cache=None):
        bootstrap = copy.deepcopy(self.bootstrap)
        bootstrap["node_id"] = node
        return CloudMemoryStore(bootstrap, self.drive,
                                cache or self.directory / "cloud-memory", self.config)

    def provider(self, store=None):
        store = store or self.store
        store.sync()
        provider = MemoryProvider(self.config)
        provider.bind_storage(store)
        provider.init_memory("robot-a", None)
        return provider

    def state(self, provider):
        return (copy.deepcopy(provider.entries), self.path.read_bytes(),
                (self.store.cache_dir / "current.json").read_bytes(),
                copy.deepcopy(self.store.current), copy.deepcopy(self.drive.manifest))

    @staticmethod
    def mutations(provider):
        return (lambda: provider.remember("New camera fact"),
                lambda: provider.forget("Camera"),
                lambda: provider.update_entry("first", "Updated camera fact"),
                lambda: provider.delete_entry("first"))

    def assert_safe(self, text):
        for private in ("private", "memory-manifest", "memory-seed", "access-token", "credentials", str(self.path)):
            self.assertNotIn(private, text)

    def test_startup_materializes_exact_scopes_and_normal_yaml(self):
        provider = self.provider()
        scopes = self.store.current["payload"]["snapshot"]["scopes"]
        self.assertEqual(yaml.safe_load(self.path.read_text()), scopes)
        self.assertEqual(provider.entries, scopes["robot-a"])
        self.assertNotIn("deskbox", scopes)
        self.assertTrue(provider.recall("Camera"))
        self.assertTrue(provider.list_entries())
        status = provider.inspect_entries()
        self.assertEqual(status["memory_revision"], 1)
        self.assertTrue(status["writable"])

    def test_reader_and_writer_use_same_device_scope(self):
        writer = self.provider()
        reader_store = self.new_store("mac2", self.directory / "reader-cache")
        reader = self.provider(reader_store)
        self.assertEqual(reader.entries, writer.entries)
        self.assertFalse(reader.inspect_entries()["writable"])
        self.assertEqual(reader.role_id, "robot-a")
        before = (copy.deepcopy(reader.entries), self.path.read_bytes(), copy.deepcopy(reader_store.current))
        for mutate in self.mutations(reader):
            with self.assertRaises(MemoryReadOnly) as raised:
                mutate()
            self.assert_safe(str(raised.exception))
            self.assertEqual((reader.entries, self.path.read_bytes(), reader_store.current), before)
        self.assertEqual(self.drive.uploads, 0)
        self.assertTrue(reader.recall("Camera"))
        self.assertTrue(reader.list_entries())

    def test_online_sync_is_hot_and_selects_stored_device_before_reconnect(self):
        self.drive.seed({"robot-a": [entry()]})
        self.store.sync()
        provider = MemoryProvider(self.config)
        provider.bind_storage(self.store)
        self.assertEqual(provider.select_stored_scope(), "robot-a")
        self.drive.seed({"robot-a": [entry(content="New camera fact")]}, revision=2)
        provider.sync_memory()
        self.assertEqual(provider.memory_revision, 2)
        self.assertEqual(provider.entries[0]["content"], "New camera fact")
        self.assertEqual(yaml.safe_load(self.path.read_text())["robot-a"], provider.entries)

    def test_offline_restart_validated_cache_restores_yaml_and_reads_need_no_fetch(self):
        self.provider()
        self.path.write_text("robot-a: []")
        self.drive.offline = True
        restarted = self.new_store()
        provider = self.provider(restarted)
        self.assertEqual(restarted.sync_state, "offline_lkg")
        calls = len(self.drive.events)
        self.assertTrue(provider.recall("Camera"))
        self.assertTrue(provider.list_entries())
        self.assertEqual(len(self.drive.events), calls)
        self.assertEqual(yaml.safe_load(self.path.read_text())["robot-a"], provider.entries)

    def test_offline_missing_or_corrupt_cache_never_trusts_arbitrary_yaml(self):
        self.path.parent.mkdir()
        self.path.write_text(yaml.safe_dump({"robot-a": [entry()]}))
        self.drive.offline = True
        with self.assertRaises(MemoryUnavailable):
            self.store.sync()
        (self.store.cache_dir / "current.json").write_text("corrupt")
        with self.assertRaises(MemoryUnavailable):
            self.new_store().sync()

    def test_cache_source_and_checksum_are_enforced(self):
        self.store.sync()
        cache = self.store.cache_dir / "current.json"
        original = json.loads(cache.read_text())
        self.drive.offline = True
        for field in ("folder_id", "memory_manifest_file_id", "snapshot", "checksum"):
            with self.subTest(field=field):
                changed = copy.deepcopy(original)
                if field == "checksum":
                    changed["sha256"] = "0" * 64
                elif field == "snapshot":
                    changed["payload"]["snapshot"]["scopes"]["robot-a"][0]["content"] = "Tampered"
                    changed["sha256"] = checksum(canonical_bytes(changed["payload"]))
                else:
                    changed["payload"][field] = "different-source"
                    changed["sha256"] = checksum(canonical_bytes(changed["payload"]))
                cache.write_bytes(canonical_bytes(changed))
                with self.assertRaises(MemoryUnavailable):
                    self.new_store().sync()

    def test_all_mutations_cas_last_then_swap_and_preserve_other_device(self):
        provider = self.provider()
        other = copy.deepcopy(self.store.scopes()["robot-b"])
        for mutate in self.mutations(provider):
            before = self.state(provider)
            previous_revision = self.drive.manifest["revision"]
            self.drive.events.clear()
            self.drive.before_cas = lambda: self.assertEqual(self.state(provider), before)
            self.assertTrue(mutate())
            self.assertEqual(self.drive.events, ["read", "download", "upload", "download", "cas"])
            self.assertEqual(self.drive.manifest["revision"], previous_revision + 1)
            self.assertEqual(provider.memory_revision, previous_revision + 1)
            self.assertEqual(self.store.scopes()["robot-b"], other)
            self.assertEqual(yaml.safe_load(self.path.read_text())["robot-a"], provider.entries)
            self.assertEqual(self.store._read_cache(), self.store.current)
        self.assertNotIn("first", [item["id"] for item in provider.entries])
        self.assertEqual(len(provider.entries), 1)
        self.assertFalse(provider.entries[0]["active"])  # Forget preserves inactive history.

    def test_all_mutations_upload_verify_and_cas_failures_are_invisible(self):
        provider = self.provider()
        for failure in ("upload", "verify", "cas", "read_race"):
            for mutate in self.mutations(provider):
                with self.subTest(failure=failure, method=mutate):
                    self.drive.failure = failure
                    before = self.state(provider)
                    expected = MemoryConflict if failure in {"cas", "read_race"} else MemoryUnavailable
                    with self.assertRaises(expected) as raised:
                        mutate()
                    self.assert_safe(str(raised.exception))
                    self.assertIsNone(raised.exception.__cause__)
                    self.assertEqual(self.state(provider), before)
        self.assertGreater(self.drive.uploads, 0)  # Failed CAS may leave orphan objects.

    def test_stale_base_revision_requires_explicit_sync_before_retry(self):
        provider = self.provider()
        before = self.state(provider)[:4]
        scopes = self.store.scopes()
        scopes["robot-b"].append(entry("next", "Another remote device fact"))
        self.drive.seed(scopes, revision=2)
        with self.assertRaises(MemoryConflict):
            provider.remember("Unsaved candidate")
        self.assertEqual(self.state(provider)[:4], before)
        self.assertEqual(self.drive.uploads, 0)
        provider.sync_memory()
        self.assertTrue(provider.remember("Saved after review"))
        self.assertEqual(self.store.status()["memory_revision"], 3)

    def test_final_cas_race_preserves_winning_manifest_and_old_local_state(self):
        provider = self.provider()
        before = self.state(provider)[:4]
        winner = []

        def win_race():
            scopes = self.store.scopes()
            scopes["robot-a"].append(entry("winner", "Concurrent winning camera fact"))
            self.drive.seed(scopes, revision=2)
            winner.append(copy.deepcopy(self.drive.manifest))

        self.drive.before_cas = win_race
        with self.assertRaises(MemoryConflict):
            provider.remember("Losing camera candidate")
        self.assertEqual(self.state(provider)[:4], before)
        self.assertEqual(self.drive.manifest, winner[0])
        self.assertEqual(self.drive.uploads, 1)
        self.assertIn("memory-upload-1", self.drive.files)

    def test_remote_base_cannot_skip_unreviewed_local_revision(self):
        self.provider()
        self.drive.seed(self.store.scopes(), revision=2)
        transform = Mock()
        with self.assertRaises(MemoryConflict):
            self.store.mutate("robot-a", 2, transform)
        transform.assert_not_called()
        self.assertEqual(self.drive.uploads, 0)

    def test_authoritative_writer_change_is_checked_before_candidate(self):
        provider = self.provider()
        self.drive.seed(self.store.scopes(), revision=2, writer="mac2")
        transform = Mock(side_effect=AssertionError("Reader candidate must not run"))
        with self.assertRaises(MemoryReadOnly):
            self.store.mutate("robot-a", 1, transform)
        transform.assert_not_called()
        self.assertEqual(provider.entries[0]["content"], "Camera uses shared I2C")

    def test_offline_rejects_every_write_but_keeps_reads(self):
        provider = self.provider()
        self.drive.offline = True
        before = self.state(provider)
        for mutate in self.mutations(provider):
            with self.assertRaises(MemoryUnavailable):
                mutate()
            self.assertEqual(self.state(provider), before)
        self.assertTrue(provider.recall("Camera"))
        self.assertTrue(provider.list_entries())
        self.assertEqual(self.drive.uploads, 0)

    def test_post_cas_local_failure_reports_success_retains_commit_and_recovers(self):
        provider = self.provider()
        before = self.path.read_bytes()
        with patch.object(self.store, "_atomic", side_effect=OSError("/private/path disk full")):
            self.assertTrue(provider.remember("Committed camera fact"))
        self.assertEqual(self.drive.manifest["revision"], 2)
        self.assertEqual(provider.memory_revision, 2)
        self.assertEqual(self.store.sync_state, "cache_error")
        self.assert_safe(self.store.last_error)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIn("Committed camera fact", [item["content"] for item in provider.entries])
        provider.init_memory("robot-a", None)
        self.assertIn("Committed camera fact", [item["content"] for item in provider.entries])
        provider.sync_memory()
        self.assertEqual(self.store.sync_state, "synced")
        self.assertEqual(self.store._read_cache(), self.store.current)
        self.assertEqual(yaml.safe_load(self.path.read_text())["robot-a"], provider.entries)

    def test_post_cas_each_local_target_can_fail_without_false_commit_failure(self):
        provider = self.provider()
        atomic = self.store._atomic
        for failed_target in (self.path, self.store.cache_dir / "current.json"):
            with self.subTest(target=failed_target.name):
                before_revision = provider.memory_revision

                def fail_one(path, content):
                    if path == failed_target:
                        raise PermissionError("private /private/path permission denied")
                    return atomic(path, content)

                with patch.object(self.store, "_atomic", side_effect=fail_one):
                    self.assertTrue(provider.remember(f"Committed fact {before_revision}"))
                self.assertEqual(provider.memory_revision, before_revision + 1)
                self.assertEqual(self.drive.manifest["revision"], before_revision + 1)
                self.assertEqual(self.store.sync_state, "cache_error")
                self.assert_safe(self.store.last_error)
                provider.sync_memory()
                self.assertEqual(self.store.sync_state, "synced")
                self.assertEqual(yaml.safe_load(self.path.read_text())["robot-a"], provider.entries)

    def test_materialization_uses_file_and_directory_fsync_and_atomic_replace(self):
        with patch("os.fsync", wraps=__import__("os").fsync) as fsync, \
                patch("os.replace", wraps=__import__("os").replace) as replace:
            self.store.sync()
        self.assertEqual(replace.call_count, 2)
        self.assertEqual(fsync.call_count, 4)
        self.assertEqual(list(self.store.cache_dir.glob(".config.*")), [])
        self.assertEqual(list(self.path.parent.glob(".config.*")), [])

    def test_invalid_remote_with_valid_cache_uses_only_validated_lkg(self):
        provider = self.provider()
        before = copy.deepcopy(provider.entries)
        self.drive.seed({}, revision=2, content=b"invalid remote snapshot")
        provider.sync_memory()
        self.assertEqual(provider.entries, before)
        self.assertEqual(provider.memory_revision, 1)
        self.assertEqual(self.store.sync_state, "offline_lkg")
        with self.assertRaises(MemoryUnavailable):
            provider.remember("Do not publish from invalid remote base")
        self.assertEqual(self.drive.uploads, 0)

    def test_noop_has_no_upload_or_revision_change(self):
        provider = self.provider()
        before = self.state(provider)
        self.assertFalse(provider.delete_entry("missing"))
        self.assertEqual(provider.forget("No matching fact"), 0)
        self.assertFalse(provider.update_entry("missing", "New fact"))
        self.assertEqual(self.state(provider), before)
        self.assertEqual(self.drive.uploads, 0)

    def test_supersession_inactive_entries_and_hard_delete_keep_model(self):
        provider = self.provider()
        provider.remember("Replacement camera fact", supersedes="first", pinned=True,
                          project="Robot", tags=[" Camera ", "camera"], importance=9)
        old, replacement = provider.entries
        self.assertFalse(old["active"])
        self.assertEqual(replacement["supersedes"], "first")
        self.assertEqual(replacement["tags"], ["Camera"])
        self.assertEqual(replacement["importance"], 5)
        self.assertEqual(self.store.scopes()["robot-a"], provider.entries)
        provider.delete_entry("first")
        self.assertEqual(provider.entries, [replacement])
        self.assertIn("memory-seed-0", self.drive.files)  # No remote GC.

    def test_rollback_revision_reuse_and_read_race_are_not_lkg_success(self):
        self.drive.seed({"robot-a": [entry()]}, revision=3)
        self.store.sync()
        current = copy.deepcopy(self.store.current)
        for mode in ("rollback", "pointer", "writer", "read_race"):
            with self.subTest(mode=mode):
                if mode == "rollback":
                    self.drive.seed({"robot-a": [entry()]}, revision=2)
                elif mode == "pointer":
                    self.drive.seed({"robot-a": [entry(content="Different")]}, revision=3)
                elif mode == "writer":
                    self.drive.manifest = copy.deepcopy(current["payload"]["manifest"])
                    self.drive.manifest["writer_node_id"] = "mac2"
                else:
                    self.drive.failure = "read_race"
                with self.assertRaises(MemoryConflict):
                    self.store.sync()
                self.assertEqual(self.store.current, current)

    def test_invalid_remote_snapshot_without_cache_fails_safely(self):
        mutations = [
            lambda obj: obj.update(extra=True),
            lambda obj: obj.update(schema_version=True),
            lambda obj: obj.update(scopes=[]),
            lambda obj: obj["scopes"].update({"": []}),
            lambda obj: obj["scopes"].update({"robot-a": {}}),
            lambda obj: obj["scopes"]["robot-a"][0].update(extra="private"),
            lambda obj: obj["scopes"]["robot-a"][0].pop("id"),
            lambda obj: obj["scopes"]["robot-a"][0].update(content="  unnormalized  "),
            lambda obj: obj["scopes"]["robot-a"][0].update(content="x" * 301),
            lambda obj: obj["scopes"]["robot-a"][0].update(type="invalid"),
            lambda obj: obj["scopes"]["robot-a"][0].update(importance=True),
            lambda obj: obj["scopes"]["robot-a"][0].update(importance=6),
            lambda obj: obj["scopes"]["robot-a"][0].update(active="false"),
            lambda obj: obj["scopes"]["robot-a"][0].update(tags=["duplicate", "Duplicate"]),
            lambda obj: obj["scopes"]["robot-a"][0].update(tags=[1]),
            lambda obj: obj["scopes"]["robot-a"].append(entry()),
        ]
        for mutate in mutations:
            obj = {"schema_version": 1, "scopes": {"robot-a": [entry()]}}
            mutate(obj)
            self.drive.seed({}, content=canonical_bytes(obj))
            with self.subTest(mutation=mutate), self.assertRaises(MemoryUnavailable) as raised:
                self.store.sync()
            self.assert_safe(str(raised.exception))
            self.assertIsNone(self.store.current)
            self.assertFalse(self.path.exists())
            self.assertFalse((self.store.cache_dir / "current.json").exists())

    def test_noncanonical_json_and_hash_mismatch_fail(self):
        obj = {"schema_version": 1, "scopes": {"robot-a": [entry()]}}
        for content in (json.dumps(obj).encode(), canonical_bytes(obj) + b"\n"):
            self.drive.seed({}, content=content)
            with self.assertRaises(MemoryUnavailable):
                self.store.sync()
        self.drive.seed(obj["scopes"])
        self.drive.manifest["snapshot"]["sha256"] = "0" * 64
        with self.assertRaises(MemoryUnavailable):
            self.store.sync()

    def test_strict_manifest_schema(self):
        valid = copy.deepcopy(self.drive.manifest)
        bad = [dict(valid, extra=1), dict(valid, schema_version=True), dict(valid, revision=True),
               dict(valid, revision=0), dict(valid, writer_node_id=" "), dict(valid, writer_node_id=1),
               dict(valid, snapshot={"file_id": "invalid/id", "sha256": "a" * 64}),
               dict(valid, snapshot={"file_id": "id", "sha256": "A" * 64}),
               dict(valid, snapshot={"file_id": "id", "sha256": "a" * 64, "extra": 1})]
        self.assertEqual(parse_manifest(canonical_bytes(valid)), valid)
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_manifest(canonical_bytes(value))

    def test_preflight_live_validation_does_not_create_files(self):
        self.store.preflight()
        self.assertFalse(self.store.cache_dir.exists())
        self.assertFalse(self.path.exists())
        self.assertIsNone(self.store.current)

    def test_preflight_empty_missing_matching_and_mismatched_yaml(self):
        self.path.parent.mkdir()
        self.store.preflight()
        for local in ({}, {"robot-a": []}, self.drive_snapshot()["scopes"]):
            self.path.write_text(yaml.safe_dump(local, sort_keys=False))
            before = self.path.read_bytes()
            self.store.preflight(source_memory=read_local_memory(self.config))
            self.assertEqual(self.path.read_bytes(), before)
            self.assertFalse(self.store.cache_dir.exists())
        for local in ({"robot-a": [entry(content="Local changed fact")]}, ["Invalid"],
                      {"robot-a": [{"content": "Meaningful legacy memory"}]}):
            self.path.write_text(yaml.safe_dump(local))
            before = self.path.read_bytes()
            with self.assertRaises(MemoryUnavailable) as raised:
                self.store.preflight(source_memory=read_local_memory(self.config))
            self.assertIn("reconciliation", str(raised.exception))
            self.assertEqual(self.path.read_bytes(), before)

    def drive_snapshot(self):
        return json.loads(self.drive.files[self.drive.manifest["snapshot"]["file_id"]])

    def test_preflight_requires_live_drive_despite_valid_cache(self):
        self.store.sync()
        before = self.path.read_bytes()
        self.drive.offline = True
        with self.assertRaises(MemoryUnavailable):
            self.store.preflight()
        self.assertEqual(self.path.read_bytes(), before)

    def test_preflight_checks_schema_races_and_missing_metadata_without_writes(self):
        for metadata in (None, "", "invalid/id", 123):
            bootstrap = copy.deepcopy(self.bootstrap)
            bootstrap["google_drive"]["memory_manifest_file_id"] = metadata
            with self.subTest(metadata=metadata), self.assertRaises(MemoryUnavailable):
                CloudMemoryStore(bootstrap, self.drive, self.store.cache_dir, self.config)
        self.drive.failure = "read_race"
        with self.assertRaises(MemoryConflict):
            self.store.preflight()
        self.drive.failure = None
        self.drive.seed({}, content=b"invalid")
        with self.assertRaises(MemoryUnavailable):
            self.store.preflight()
        self.assertFalse(self.store.cache_dir.exists())
        self.assertFalse(self.path.exists())


class LocalMemoryTests(unittest.TestCase):
    def test_existing_yaml_all_operations_atomic_save_and_zero_cloud(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        path = directory / "memory.yaml"
        scopes = {"robot-a": [entry()], "robot-b": [entry("other", "Other device")]}
        path.write_text(yaml.safe_dump(scopes))
        from config.config_store import LocalConfigStoreAdapter
        store = LocalConfigStoreAdapter({"config_provider": "local", "node_id": "mac2"},
                                        local_path=directory / ".config.yaml")
        with patch("config.cloud_memory.CloudMemoryStore", side_effect=AssertionError("Cloud construction")), \
                patch("config.drive_transport.GoogleDriveTransport", side_effect=AssertionError("Drive construction")):
            provider = MemoryProvider({"path": str(path), "recall_min_score": 0.1})
            provider.bind_storage(store.memory_storage(provider.config))
            self.assertEqual(provider.select_stored_scope(), None)  # Two device scopes.
            provider.init_memory("robot-a", None)
            self.assertEqual(provider.entries, scopes["robot-a"])
            self.assertTrue(provider.recall("Camera"))
            self.assertTrue(provider.list_entries())
            with patch("os.replace", wraps=__import__("os").replace) as replace, \
                    patch("os.fsync", wraps=__import__("os").fsync) as fsync:
                self.assertTrue(provider.remember("New local camera fact"))
                self.assertEqual(provider.forget("shared I2C"), 1)
                self.assertTrue(provider.update_entry("first", "Updated local camera fact", active=True))
                self.assertTrue(provider.delete_entry("first"))
                self.assertEqual(replace.call_count, 4)
                self.assertGreaterEqual(fsync.call_count, 4)
            saved = yaml.safe_load(path.read_text())
            self.assertEqual(saved["robot-a"], provider.entries)
            self.assertEqual(saved["robot-b"], scopes["robot-b"])
            self.assertEqual(provider.inspect_entries()["storage_source"], "local")
        self.assertFalse((directory / "cloud-memory").exists())
        self.assertEqual(list(directory.glob(".memory-*.tmp")), [])

    def test_local_single_scope_admin_and_recall_settings_remain_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.yaml"
            path.write_text(yaml.safe_dump({"robot-a": [entry(pinned=True)]}))
            provider = MemoryProvider({"path": str(path), "recall_enabled": False})
            self.assertEqual(provider.select_stored_scope(), "robot-a")
            self.assertIn("Pinned memories:", provider.recall("Unrelated"))
            provider.remember("Camera uses shared I2C", pinned=False)
            self.assertEqual(len(provider.entries), 1)
            self.assertEqual(provider.recall("Camera"), "")


class CloudMemoryIntegrationTests(unittest.TestCase):
    new_cloud = config_tests.CloudConfigTests.new_cloud

    def setUp(self):
        config_tests.CloudConfigTests.setUp(self)
        self.path = self.directory / "configured-memory.yaml"
        self.defaults["selected_module"]["Memory"] = "ExplicitAlias"
        self.defaults["Memory"] = {"ExplicitAlias": {"type": "mem_local_explicit", "path": str(self.path)}}
        self.default_path.write_text(yaml.safe_dump(self.defaults))
        self.drive.publish(self.defaults, self.cloud_overrides, 1)
        self.drive = SnapshotDrive(self.drive)
        self.drive.seed({"robot-a": [entry()]}, writer="test-node")
        self.bootstrap["google_drive"]["memory_manifest_file_id"] = "memory-manifest"
        self.store = self.new_cloud()

    def test_cloud_boot_syncs_before_provider_creation_independent_config_revision(self):
        effective = self.store.prepare_runtime()
        self.assertTrue(self.path.exists())
        storage = self.store.memory_storage(effective["Memory"]["ExplicitAlias"])
        provider = MemoryProvider(effective["Memory"]["ExplicitAlias"])
        provider.bind_storage(storage)
        provider.init_memory("robot-a", None)
        self.store.mark_applied()
        desired = self.store.revision_unlocked()
        active = self.store.active_revision
        provider.remember("Hot camera fact")
        self.assertEqual(storage.status()["memory_revision"], 2)
        self.assertEqual(self.store.revision_unlocked(), desired)
        self.assertEqual(self.store.active_revision, active)
        self.assertEqual(self.drive.config_drive.uploads, 0)

    def test_cloud_config_active_lkg_and_memory_current_are_independent(self):
        self.store.prepare_runtime()
        self.store.mark_applied()
        provider = MemoryProvider(self.defaults["Memory"]["ExplicitAlias"])
        provider.bind_storage(self.store.memory_store)
        provider.init_memory("robot-a", None)
        provider.remember("Hot memory revision two")
        self.path.unlink()
        self.drive.offline = True
        restarted = self.new_cloud()
        restarted.prepare_runtime()
        self.assertEqual(restarted.runtime_source, "active_lkg")
        self.assertEqual(restarted.memory_store.status()["memory_revision"], 2)
        self.assertEqual(restarted.memory_store.sync_state, "offline_lkg")
        self.assertIn("Hot memory revision two", self.path.read_text())

    def test_missing_metadata_fails_explicit_alias_readiness_and_runtime(self):
        del self.bootstrap["google_drive"]["memory_manifest_file_id"]
        store = self.new_cloud()
        for operation in (store.prepare_runtime, store.validate_runtime_readiness):
            with self.assertRaises(ConfigUnavailable) as raised:
                operation()
            self.assertEqual(str(raised.exception), MemoryUnavailable.message)
            self.assertIsNone(store.runtime_snapshot)
        self.assertFalse(self.path.exists())
        self.assertFalse((self.directory / "cloud-memory").exists())

    def test_nonexplicit_memory_does_not_require_metadata_or_create_cloud_memory(self):
        del self.bootstrap["google_drive"]["memory_manifest_file_id"]
        self.defaults["Memory"]["ExplicitAlias"]["type"] = "nomem"
        self.drive.config_drive.publish(self.defaults, self.cloud_overrides, 2)
        self.default_path.write_text(yaml.safe_dump(self.defaults))
        store = self.new_cloud()
        store.validate_runtime_readiness()
        store.prepare_runtime()
        self.assertIsNone(store.memory_store)
        self.assertFalse(self.path.exists())
        self.assertFalse((self.directory / "cloud-memory").exists())

    def test_source_switch_preflight_guard_preserves_bootstrap_and_memory(self):
        from config.config_store import stage_provider_switch
        local = config_tests.CloudConfigTests.local(self)
        local.bootstrap["google_drive"] = self.bootstrap["google_drive"]
        bootstrap_path = self.directory / "bootstrap.yaml"
        bootstrap_path.write_text(yaml.safe_dump(local.bootstrap))
        original = bootstrap_path.read_bytes()
        self.path.write_text(yaml.safe_dump({"robot-a": [entry(content="Meaningful local change")]}))
        memory_before = self.path.read_bytes()
        with patch("config.config_store.create_config_store", return_value=self.store), \
                patch("config.config_store.get_config_store", return_value=local), \
                patch("config.config_store.load_bootstrap", return_value=local.bootstrap), \
                patch("config.config_store.save_bootstrap",
                      side_effect=lambda value: bootstrap_path.write_text(yaml.safe_dump(value))):
            with self.assertRaises(ConfigUnavailable) as raised:
                stage_provider_switch("google_drive")
            self.assertIn("reconciliation", str(raised.exception))
            self.assertEqual(bootstrap_path.read_bytes(), original)
            self.assertEqual(self.path.read_bytes(), memory_before)
            self.path.write_text(yaml.safe_dump({"robot-a": [entry()]}))
            memory_before = self.path.read_bytes()
            stage_provider_switch("google_drive")
        self.assertEqual(self.path.read_bytes(), memory_before)
        self.assertFalse((self.directory / "cloud-memory").exists())

    def test_cloud_to_local_retains_yaml_and_local_edits_block_switch_back(self):
        self.store.prepare_runtime()
        original = self.path.read_bytes()
        provider = MemoryProvider(self.defaults["Memory"]["ExplicitAlias"])
        provider.init_memory("robot-a", None)
        self.assertEqual(self.path.read_bytes(), original)
        provider.remember("New Local fact")
        before = self.path.read_bytes()
        with self.assertRaises(ConfigUnavailable):
            self.new_cloud().validate_runtime_readiness(source_memory=read_local_memory(provider.config))
        self.assertEqual(self.path.read_bytes(), before)

    def test_module_initialization_binds_prepared_store_and_local_none(self):
        with patch("config.logger.setup_logging", return_value=Mock()):
            from core.utils.modules_initialize import initialize_modules
        effective = self.store.prepare_runtime()
        with patch("config.config_store.get_config_store", return_value=self.store):
            modules = initialize_modules(Mock(), effective, init_memory=True)
        self.assertIs(modules["memory"].storage, self.store.memory_store)
        local = config_tests.CloudConfigTests.local(self)
        with patch("config.config_store.get_config_store", return_value=local), \
                patch("config.cloud_memory.CloudMemoryStore", side_effect=AssertionError("Cloud construction")):
            modules = initialize_modules(Mock(), effective, init_memory=True)
        self.assertIsNone(modules["memory"].storage)


class MemorySourceSwitchTests(unittest.TestCase):
    """Preflight reads the active source, and never rewrites either memory path."""

    new_cloud = config_tests.CloudConfigTests.new_cloud

    def setUp(self):
        CloudMemoryIntegrationTests.setUp(self)
        self.source_path = self.directory / "custom-local-memory.yaml"
        self.observed_paths = {self.source_path, self.path}
        self.drive.seed({"robot-a": [entry()]}, writer="test-node")

    def local_source(self, *, path=None, max_chars=500, explicit=True):
        local = config_tests.CloudConfigTests.local(self)
        local.bootstrap["google_drive"] = self.bootstrap["google_drive"]
        overrides = copy.deepcopy(self.overrides)
        overrides["selected_module"] = {"Memory": "LocalAlias"}
        overrides["Memory"] = {"LocalAlias": {
            "type": "mem_local_explicit" if explicit else "nomem",
            "path": str(path or self.source_path), "entry_max_chars": max_chars,
        }}
        local.local.local_path.write_text(yaml.safe_dump(overrides))
        local.prepare_runtime()
        return local

    def cloud_limits(self, max_chars):
        self.defaults["Memory"]["ExplicitAlias"]["entry_max_chars"] = max_chars
        self.default_path.write_text(yaml.safe_dump(self.defaults))
        revision = self.drive.config_drive.manifest["revision"] + 1
        self.drive.config_drive.publish(self.defaults, self.cloud_overrides, revision)
        self.store = self.new_cloud()

    def unchanged_state(self):
        cache = self.directory / "cloud-memory"
        return (
            {str(path): path.read_bytes() if path.exists() else None for path in self.observed_paths},
            cache.exists(),
            {str(path.relative_to(cache)): path.read_bytes() for path in cache.rglob("*") if path.is_file()},
            copy.deepcopy(self.drive.manifest), self.drive.uploads,
        )

    def switch(self, current, candidate, *, reject=False):
        from config.config_store import stage_provider_switch
        destination = candidate.bootstrap["config_provider"]
        bootstrap_path = self.directory / "bootstrap.yaml"
        bootstrap_path.write_text(yaml.safe_dump(current.bootstrap))
        bootstrap_before = bootstrap_path.read_bytes()
        before = self.unchanged_state()
        with patch("config.config_store.get_config_store", return_value=current), \
                patch("config.config_store.load_bootstrap", return_value=current.bootstrap), \
                patch("config.config_store.create_config_store", return_value=candidate), \
                patch("config.config_store.save_bootstrap") as save:
            if reject:
                with self.assertRaises(ConfigUnavailable) as raised:
                    stage_provider_switch(destination)
                self.assertIn("reconciliation", str(raised.exception))
                for value in (str(self.source_path), str(self.path), "Camera uses shared I2C"):
                    self.assertNotIn(value, str(raised.exception))
                save.assert_not_called()
            else:
                stage_provider_switch(destination)
                # Actual selection is published only after preflight passes.
                # Mock that final publication to prove readiness itself is read-only.
                save.assert_called_once()
                self.assertEqual(save.call_args.args[0]["config_provider"], destination)
        self.assertEqual(bootstrap_path.read_bytes(), bootstrap_before)
        self.assertEqual(self.unchanged_state(), before)

    def test_different_source_and_cloud_paths_reject_differing_authoritative_local_state(self):
        self.source_path.write_text(yaml.safe_dump({"robot-a": [entry(content="Meaningful Local fact")]}))
        local = self.local_source()
        for destination in (None, "{}"):
            with self.subTest(destination=destination):
                if destination is not None:
                    self.path.write_text(destination)
                self.switch(local, self.new_cloud(), reject=True)

    def test_matching_source_at_different_path_passes_without_reading_destination_yaml(self):
        self.source_path.write_text(yaml.safe_dump({"robot-a": [entry()]}, sort_keys=False))
        local = self.local_source()
        for destination in (None, "{}", "Unrelated stale destination memory"):
            with self.subTest(destination=destination):
                if destination is not None:
                    self.path.write_text(destination)
                self.switch(local, self.new_cloud())

    def test_cloud_limit_cannot_truncate_local_long_content_into_false_match(self):
        long_entry = entry()
        long_entry["content"] = "x" * 400
        truncated = dict(long_entry, content=long_entry["content"][:300])
        self.source_path.write_text(yaml.safe_dump({"robot-a": [long_entry]}))
        self.drive.seed({"robot-a": [truncated]}, writer="test-node")
        local = self.local_source(max_chars=500)
        self.switch(local, self.new_cloud(), reject=True)

    def test_matching_long_content_passes_with_capable_cloud_schema(self):
        self.cloud_limits(500)
        long_entry = entry()
        long_entry["content"] = "x" * 400
        self.source_path.write_text(yaml.safe_dump({"robot-a": [long_entry]}))
        self.drive.seed({"robot-a": [long_entry]}, writer="test-node")
        self.switch(self.local_source(max_chars=500), self.new_cloud())

    def test_active_local_alias_path_and_limits_survive_unapplied_config_changes(self):
        long_entry = entry()
        long_entry["content"] = "x" * 400
        self.source_path.write_text(yaml.safe_dump({"robot-a": [long_entry]}))
        local = self.local_source(max_chars=500)
        # Saved-but-unapplied settings select the Cloud destination path and
        # a smaller limit. The currently running Local provider still uses A/500.
        overrides = yaml.safe_load(local.local.local_path.read_text())
        overrides["Memory"]["LocalAlias"].update(path=str(self.path), entry_max_chars=300)
        local.local.local_path.write_text(yaml.safe_dump(overrides))
        self.path.write_text("{}")
        truncated = dict(long_entry, content=long_entry["content"][:300])
        self.drive.seed({"robot-a": [truncated]}, writer="test-node")
        self.switch(local, self.new_cloud(), reject=True)
        self.cloud_limits(500)
        self.drive.seed({"robot-a": [long_entry]}, writer="test-node")
        self.switch(local, self.new_cloud())

    def test_nonexplicit_local_source_ignores_unrelated_stale_yaml(self):
        self.source_path.write_text("malformed: [stale")
        self.path.write_text(yaml.safe_dump({"robot-a": [entry(content="Unrelated destination fact")]}))
        local = self.local_source(explicit=False)
        self.switch(local, self.new_cloud())

    def test_lossy_legacy_or_manual_values_require_reconciliation(self):
        local = self.local_source()
        values = [dict(entry(), content="  Camera uses shared I2C  "),
                  dict(entry(), tags=["Camera", "camera"]), dict(entry(), active=1),
                  dict(entry(), title="Meaningful provenance"), {"content": "Camera uses shared I2C"},
                  dict(entry(), content="x" * 501)]
        for value in values:
            with self.subTest(value=value):
                self.source_path.write_text(yaml.safe_dump({"robot-a": [value]}))
                self.switch(local, self.new_cloud(), reject=True)

    def test_empty_or_missing_explicit_local_source_can_switch(self):
        local = self.local_source()
        self.switch(local, self.new_cloud())
        for empty in ("", "{}", "robot-a: []"):
            with self.subTest(empty=empty):
                self.source_path.write_text(empty)
                self.switch(local, self.new_cloud())

    def test_nonexplicit_cloud_candidate_cannot_hide_meaningful_local_memory(self):
        self.source_path.write_text(yaml.safe_dump({"robot-a": [entry()]}))
        local = self.local_source()
        self.defaults["Memory"]["ExplicitAlias"]["type"] = "nomem"
        self.default_path.write_text(yaml.safe_dump(self.defaults))
        self.drive.config_drive.publish(self.defaults, self.cloud_overrides, 1)
        self.switch(local, self.new_cloud(), reject=True)

    def test_cloud_to_local_different_path_requires_exact_matching_destination(self):
        self.store.prepare_runtime()
        local = self.local_source()
        self.switch(self.store, local, reject=True)
        self.source_path.write_text("{}")
        self.switch(self.store, local, reject=True)
        self.source_path.write_text(yaml.safe_dump({"robot-a": [entry(content="Different Local state")]}))
        self.switch(self.store, local, reject=True)
        self.source_path.write_text(yaml.safe_dump({"robot-a": [entry()]}))
        self.switch(self.store, local)

    def test_cloud_to_local_same_materialized_path_preserves_dataset(self):
        self.store.prepare_runtime()
        local = self.local_source(path=self.path)
        self.switch(self.store, local)

    def test_cloud_to_nonexplicit_local_candidate_rejects_meaningful_cloud_state(self):
        self.store.prepare_runtime()
        self.switch(self.store, self.local_source(explicit=False), reject=True)

    def test_cloud_to_local_cannot_truncate_long_cloud_records(self):
        self.cloud_limits(500)
        long_entry = entry()
        long_entry["content"] = "x" * 400
        self.drive.seed({"robot-a": [long_entry]}, writer="test-node")
        self.store.prepare_runtime()
        self.source_path.write_text(yaml.safe_dump({"robot-a": [long_entry]}))
        self.switch(self.store, self.local_source(max_chars=300), reject=True)
        self.switch(self.store, self.local_source(max_chars=500))

    def test_reverse_switch_uses_committed_hot_state_after_yaml_write_failure(self):
        self.store.prepare_runtime()
        provider = MemoryProvider(self.defaults["Memory"]["ExplicitAlias"])
        provider.bind_storage(self.store.memory_store)
        provider.init_memory("robot-a", None)
        with patch.object(self.store.memory_store, "_atomic", side_effect=OSError("Disk full")):
            provider.remember("Committed hot fact")
        # YAML still holds revision one. It must not authorize a Local switch
        # that would discard the committed revision-two memory.
        local = self.local_source(path=self.path)
        self.switch(self.store, local, reject=True)


class MemoryAPIToolTests(unittest.TestCase):
    setUp = CloudMemoryTests.setUp
    new_store = CloudMemoryTests.new_store
    provider = CloudMemoryTests.provider
    assert_safe = CloudMemoryTests.assert_safe

    def handler(self, provider):
        config = {"selected_module": {"Memory": "ExplicitAlias"},
                  "Memory": {"ExplicitAlias": {"type": "mem_local_explicit"}},
                  "server": {"settings": {"allow_remote": True}}}
        with patch("core.api.base_handler.setup_logging", return_value=Mock()):
            return MemoryHandler(config, provider)

    @staticmethod
    def request(body=None, identifier="first"):
        return SimpleNamespace(content_type="application/json", match_info={"entry_id": identifier},
                               json=AsyncMock(return_value=body or {"content": "New camera fact"}))

    def test_settings_writer_post_put_delete_use_shared_transaction(self):
        provider = self.provider()
        handler = self.handler(provider)
        response = asyncio.run(handler.handle_post(self.request()))
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.body)["memory_revision"], 2)
        response = asyncio.run(handler.handle_put(self.request({"content": "Updated camera fact"})))
        self.assertEqual(response.status, 200)
        response = asyncio.run(handler.handle_delete(self.request()))
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.body)["memory_revision"], 4)
        self.assertEqual(self.drive.manifest["revision"], 4)

    def test_settings_domain_errors_are_controlled_for_all_mutation_routes(self):
        provider = self.provider()
        handler = self.handler(provider)
        for domain, status in ((MemoryReadOnly, 409), (MemoryConflict, 409), (MemoryUnavailable, 503),
                               (lambda: OSError("private /private/credentials.json access-token"), 503)):
            for method, route in (("remember", handler.handle_post), ("update_entry", handler.handle_put),
                                  ("delete_entry", handler.handle_delete)):
                with self.subTest(error=domain, method=method):
                    with patch.object(provider, method, side_effect=domain()):
                        with self.assertRaises(web.HTTPException) as raised:
                            asyncio.run(route(self.request()))
                    self.assertEqual(raised.exception.status, status)
                    self.assert_safe(raised.exception.text)
        self.assertEqual(self.drive.manifest["revision"], 1)

    def test_get_syncs_hot_reader_selects_stored_scope_and_supports_offline_reads(self):
        self.drive.seed({"robot-a": [entry()]})
        reader_store = self.new_store("mac2", self.directory / "reader-cache")
        reader_store.sync()
        provider = MemoryProvider(self.config)
        provider.bind_storage(reader_store)
        handler = self.handler(provider)
        response = asyncio.run(handler.handle_get(self.request()))
        snapshot = json.loads(response.body)
        self.assertEqual(snapshot["device_id"], "robot-a")
        self.assertFalse(snapshot["writable"])
        self.drive.seed({"robot-a": [entry(content="Latest remote camera fact")]}, revision=2)
        snapshot = json.loads(asyncio.run(handler.handle_get(self.request())).body)
        self.assertEqual(snapshot["memory_revision"], 2)
        self.assertEqual(snapshot["entries"][0]["content"], "Latest remote camera fact")
        self.drive.offline = True
        snapshot = json.loads(asyncio.run(handler.handle_get(self.request())).body)
        self.assertEqual(snapshot["sync_state"], "offline_lkg")
        self.assertEqual(snapshot["memory_revision"], 2)

    def test_get_preserves_real_manifest_race_as_http_conflict(self):
        provider = self.provider()
        self.drive.failure = "read_race"
        with self.assertRaises(web.HTTPConflict) as raised:
            asyncio.run(self.handler(provider).handle_get(self.request()))
        self.assert_safe(raised.exception.text)
        self.assertEqual(provider.memory_revision, 1)

    def test_post_cas_cache_failure_api_reports_committed_success(self):
        provider = self.provider()
        handler = self.handler(provider)
        with patch.object(self.store, "_atomic", side_effect=OSError("private disk error")):
            response = asyncio.run(handler.handle_post(self.request()))
        self.assertEqual(response.status, 200)
        snapshot = json.loads(response.body)
        self.assertEqual(snapshot["memory_revision"], 2)
        self.assertEqual(snapshot["sync_state"], "cache_error")
        self.assert_safe(snapshot["storage_error"])

    def test_manage_memory_remember_and_forget_catch_every_storage_error(self):
        provider = self.provider()
        handler = self.handler(provider)
        conn = SimpleNamespace(config=handler.config, memory=provider)
        for action in ("remember", "forget"):
            for error in (MemoryReadOnly(), MemoryConflict(), MemoryUnavailable(),
                          OSError("private /private/credentials.json access-token")):
                with self.subTest(action=action, error=type(error)):
                    with patch.object(provider, action, side_effect=error):
                        response = asyncio.run(manage_memory(conn, action, "Camera"))
                    self.assertEqual(response.action, Action.ERROR)
                    self.assert_safe(response.response)
        self.assertEqual(self.drive.manifest["revision"], 1)

    def test_manage_memory_writer_reader_and_offline_recall_list(self):
        provider = self.provider()
        conn = SimpleNamespace(config=self.handler(provider).config, memory=provider)
        response = asyncio.run(manage_memory(conn, "remember", "New tool camera fact"))
        self.assertEqual(response.action, Action.RESPONSE)
        response = asyncio.run(manage_memory(conn, "forget", "New tool camera fact"))
        self.assertEqual(response.action, Action.RESPONSE)
        self.assertEqual(self.drive.manifest["revision"], 3)
        self.drive.offline = True
        for action in ("remember", "forget"):
            self.assertEqual(asyncio.run(manage_memory(conn, action, "Camera")).action, Action.ERROR)
        self.assertEqual(asyncio.run(manage_memory(conn, "recall", "Camera")).action, Action.REQLLM)
        self.assertEqual(asyncio.run(manage_memory(conn, "list")).action, Action.RESPONSE)

    def test_manage_memory_alias_preserves_configured_responses(self):
        provider = self.provider()
        config = self.handler(provider).config
        config["Memory"]["ExplicitAlias"]["responses"] = {"remembered": "Saved explicit fact."}
        conn = SimpleNamespace(config=config, memory=provider)
        self.assertEqual(asyncio.run(manage_memory(conn, "remember", "New camera fact")).response,
                         "Saved explicit fact.")
