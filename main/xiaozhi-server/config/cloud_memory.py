"""Independent, single-writer Cloud Memory hot state with validated read LKG."""

import copy
import json
import logging
import re
import threading
from contextlib import contextmanager
from pathlib import Path

import portalocker
import yaml

from config.config_store import ConfigConflict, canonical_bytes, checksum
from config.local_config import LocalConfigStore
from config.memory_reconciliation import explicit_memory_config, memory_path, require_memory_match
from core.memory_schema import EntryNormalization, _MEMORY_TYPES
from core.memory_storage import (
    MemoryConflict, MemoryReadOnly,
    MemoryUnavailable,
)

_ENTRY_FIELDS = {
    "id", "content", "type", "project", "entities", "tags", "importance",
    "pinned", "active", "supersedes", "created_at", "updated_at",
}


def parse_manifest(content):
    value = json.loads(content)
    if (not isinstance(value, dict)
            or set(value) != {"schema_version", "revision", "writer_node_id", "snapshot"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or type(value["revision"]) is not int or value["revision"] < 1
            or not isinstance(value["writer_node_id"], str)
            or not value["writer_node_id"].strip()):
        raise ValueError("Invalid memory manifest")
    pointer = value["snapshot"]
    if (not isinstance(pointer, dict) or set(pointer) != {"file_id", "sha256"}
            or not isinstance(pointer["file_id"], str)
            or not re.fullmatch(r"[a-zA-Z0-9_-]+", pointer["file_id"])
            or not isinstance(pointer["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", pointer["sha256"])):
        raise ValueError("Invalid memory pointer")
    return value


class MemorySchema(EntryNormalization):
    def __init__(self, config):
        self.entry_max_chars = max(50, int(config.get("entry_max_chars", 300)))

    def validate(self, snapshot):
        if (not isinstance(snapshot, dict) or set(snapshot) != {"schema_version", "scopes"}
                or type(snapshot["schema_version"]) is not int
                or snapshot["schema_version"] != 1 or not isinstance(snapshot["scopes"], dict)):
            raise ValueError("Invalid memory snapshot")
        for scope, entries in snapshot["scopes"].items():
            if not isinstance(scope, str) or not scope.strip() or not isinstance(entries, list):
                raise ValueError("Invalid memory scope")
            ids = set()
            for entry in entries:
                if not isinstance(entry, dict) or set(entry) != _ENTRY_FIELDS:
                    raise ValueError("Invalid memory entry")
                for field in ("id", "content", "type", "created_at", "updated_at"):
                    if not isinstance(entry[field], str) or not entry[field].strip():
                        raise ValueError("Invalid memory text")
                if (entry["id"] in ids or entry["type"] not in _MEMORY_TYPES
                        or type(entry["importance"]) is not int
                        or not 1 <= entry["importance"] <= 5
                        or type(entry["active"]) is not bool or type(entry["pinned"]) is not bool):
                    raise ValueError("Invalid memory metadata")
                ids.add(entry["id"])
                for field in ("project", "supersedes"):
                    if entry[field] is not None and not isinstance(entry[field], str):
                        raise ValueError("Invalid optional memory text")
                for field in ("entities", "tags"):
                    if not isinstance(entry[field], list) or not all(
                        isinstance(item, str) for item in entry[field]
                    ):
                        raise ValueError("Invalid memory list")
                # Cloud data must already conform to the existing model. Never repair it.
                if self._normalize_entry(entry) != entry:
                    raise ValueError("Unnormalized memory entry")
        return snapshot

    def validate_object(self, content, manifest):
        if checksum(content) != manifest["snapshot"]["sha256"]:
            raise ValueError("Memory checksum mismatch")
        snapshot = self.validate(json.loads(content))
        if canonical_bytes(snapshot) != content:
            raise ValueError("Memory snapshot must be canonical JSON")
        return snapshot


class CloudMemoryStore:
    def __init__(self, bootstrap, transport, cache_dir, provider_config):
        drive = bootstrap.get("google_drive", {})
        manifest_id = drive.get("memory_manifest_file_id")
        if not isinstance(manifest_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", manifest_id):
            raise MemoryUnavailable()
        self.folder_id = drive["folder_id"]
        self.manifest_id = manifest_id
        self.node_id = bootstrap["node_id"]
        self.transport = transport
        self.cache_dir = Path(cache_dir)
        self.path = memory_path(provider_config)
        # A materialized YAML must never alias the authority cache/lock.
        if self.path.resolve().is_relative_to(self.cache_dir.resolve()):
            raise MemoryUnavailable()
        self.schema = MemorySchema(provider_config)
        self.thread_lock = threading.RLock()
        self.current = None
        self.sync_state = "not_synced"
        self.last_error = None

    @contextmanager
    def locked(self):
        try:
            self.cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            with self.thread_lock, portalocker.Lock(
                str(self.cache_dir / ".cloud-memory.lock"), mode="a", timeout=5
            ):
                yield
        except (OSError, portalocker.exceptions.LockException):
            raise MemoryUnavailable() from None

    def _envelope(self, manifest, snapshot):
        payload = {"folder_id": self.folder_id, "memory_manifest_file_id": self.manifest_id,
                   "manifest": manifest, "snapshot": snapshot}
        return {"payload": payload, "sha256": checksum(canonical_bytes(payload))}

    def _read_cache(self):
        path = self.cache_dir / "current.json"
        if path.is_symlink():
            raise ValueError("Invalid memory cache")
        envelope = json.loads(path.read_bytes())
        if not isinstance(envelope, dict) or set(envelope) != {"payload", "sha256"}:
            raise ValueError("Invalid memory cache envelope")
        payload = envelope["payload"]
        if (not isinstance(payload, dict)
                or set(payload) != {"folder_id", "memory_manifest_file_id", "manifest", "snapshot"}
                or payload["folder_id"] != self.folder_id
                or payload["memory_manifest_file_id"] != self.manifest_id
                or checksum(canonical_bytes(payload)) != envelope["sha256"]):
            raise ValueError("Memory cache integrity/source mismatch")
        manifest = parse_manifest(canonical_bytes(payload["manifest"]))
        snapshot = self.schema.validate_object(canonical_bytes(payload["snapshot"]), manifest)
        return self._envelope(manifest, snapshot)

    @staticmethod
    def _check_floor(manifest, previous):
        if previous is None:
            return
        old = previous["payload"]["manifest"]
        if (manifest["revision"] < old["revision"]
                or (manifest["revision"] == old["revision"] and manifest != old)):
            raise MemoryConflict()

    def _known_current(self):
        try:
            cached = self._read_cache()
        except (OSError, ValueError, TypeError, KeyError):
            cached = None
        if cached is None:
            return self.current
        if self.current is not None:
            older, newer = sorted((cached, self.current),
                                  key=lambda item: item["payload"]["manifest"]["revision"])
            self._check_floor(newer["payload"]["manifest"], older)
            return newer
        return cached

    def _fetch(self, previous):
        try:
            content, etag = self.transport.read_manifest(self.manifest_id)
            manifest = parse_manifest(content)
            if not isinstance(etag, str) or not etag or etag == "*":
                raise ValueError("Invalid memory ETag")
            snapshot = self.schema.validate_object(
                self.transport.download(manifest["snapshot"]["file_id"]), manifest
            )
            self._check_floor(manifest, previous)
            return self._envelope(manifest, snapshot), etag
        except (ConfigConflict, MemoryConflict):
            raise MemoryConflict() from None
        except Exception:
            # Provider/auth/JSON/filesystem diagnostics never cross the boundary.
            raise MemoryUnavailable() from None

    @staticmethod
    def _atomic(path, content):
        if path.is_symlink():
            raise OSError("Invalid materialization target")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        LocalConfigStore(path.parent / ".config.yaml")._atomic_bytes(path, content)

    def _persist(self, envelope, committed=False):
        failed = False
        for path, content in (
            (self.cache_dir / "current.json", canonical_bytes(envelope)),
            (self.path, yaml.safe_dump(envelope["payload"]["snapshot"]["scopes"],
                                       allow_unicode=True, sort_keys=False).encode("utf-8")),
        ):
            try:
                self._atomic(path, content)
            except OSError:
                failed = True
        if failed:
            self.sync_state = "cache_error"
            state = "committed" if committed else "resolved"
            self.last_error = f"Cloud Memory {state}; local persistence unavailable; sync to recover"
            if committed:
                logging.getLogger(__name__).warning(self.last_error)
            else:
                raise MemoryUnavailable() from None
        else:
            self.last_error = None

    def sync(self):
        with self.locked():
            previous = self._known_current()
            try:
                envelope, _ = self._fetch(previous)
            except MemoryUnavailable:
                if previous is None:
                    raise
                envelope = previous
                self.sync_state = "offline_lkg"
            else:
                self.sync_state = "synced"
            self._persist(envelope)
            self.current = envelope
            return self.status()

    def preflight(self, source_memory=None):
        """Live readiness only: never mkdir, write cache or materialize YAML."""
        with self.thread_lock:
            envelope, _ = self._fetch(self._known_current())
            require_memory_match(source_memory, envelope["payload"]["snapshot"]["scopes"],
                                 allow_empty_source=True)
            return copy.deepcopy(envelope["payload"])

    def scopes(self):
        with self.thread_lock:
            if self.current is None:
                raise MemoryUnavailable()
            return copy.deepcopy(self.current["payload"]["snapshot"]["scopes"])

    def status(self):
        with self.thread_lock:
            manifest = self.current["payload"]["manifest"] if self.current else {}
            return {"storage_source": "google_drive", "memory_revision": manifest.get("revision"),
                    "writer_node_id": manifest.get("writer_node_id"),
                    "writable": manifest.get("writer_node_id") == self.node_id,
                    "sync_state": self.sync_state, "storage_error": self.last_error}

    def mutate(self, scope, base_revision, transform):
        with self.locked():
            previous = self._known_current()
            envelope, etag = self._fetch(previous)
            manifest = envelope["payload"]["manifest"]
            if manifest["writer_node_id"] != self.node_id:
                raise MemoryReadOnly()
            if (self.current is None or type(base_revision) is not int
                    or manifest["revision"] != base_revision
                    or self.current["payload"]["manifest"]["revision"] != base_revision):
                raise MemoryConflict()
            if not isinstance(scope, str) or not scope.strip():
                raise MemoryConflict()
            candidate = copy.deepcopy(envelope["payload"]["snapshot"])
            entries = candidate["scopes"].setdefault(scope, [])
            result = transform(entries)
            if not result:
                return result, copy.deepcopy(entries)
            try:
                self.schema.validate(candidate)
                content = canonical_bytes(candidate)
                digest = checksum(content)
                next_manifest = {"schema_version": 1, "revision": manifest["revision"] + 1,
                                 "writer_node_id": manifest["writer_node_id"],
                                 "snapshot": {"file_id": "pending", "sha256": digest}}
                file_id = self.transport.upload_immutable(
                    self.folder_id, content, f"memory-{next_manifest['revision']}-{digest}.json"
                )
                next_manifest["snapshot"]["file_id"] = file_id
                parse_manifest(canonical_bytes(next_manifest))
                self.schema.validate_object(self.transport.download(file_id), next_manifest)
                # Publication is the last remote operation; all local/live state is still old.
                self.transport.replace_manifest(self.manifest_id, canonical_bytes(next_manifest), etag)
            except ConfigConflict:
                raise MemoryConflict() from None
            except Exception:
                raise MemoryUnavailable() from None
            self.current = self._envelope(next_manifest, candidate)
            self.sync_state = "synced"
            # A successful CAS is authoritative even if either local write fails.
            self._persist(self.current, committed=True)
            return result, copy.deepcopy(entries)
