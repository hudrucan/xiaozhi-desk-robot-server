"""Immutable cloud layers + conditional manifest commit + validated local LKG."""

import copy
import json
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path

import portalocker

from config.config_loader import get_project_dir, load_default_config, merge_configs
from config.cloud_secrets import LocalSecretStore
from config.cloud_soundbank import CloudSoundbankAssets
from config.cloud_memory import CloudMemoryStore, explicit_memory_config, memory_path
from config.memory_reconciliation import require_memory_match
from core.memory_storage import MemoryConflict, MemoryStorageError, MemoryUnavailable
from config.cloud_layers import (
    centralized, node_assignment, resolve_layers, update_node_overrides, validate_layers,
)
from config.config_store import (
    ConfigConflict, ConfigStore, ConfigUnavailable, PreparedConfig, canonical_bytes, checksum,
)
from config.drive_transport import GoogleDriveTransport
from config.local_config import LocalConfigStore


def parse_manifest(content):
    value = json.loads(content)
    if (not isinstance(value, dict) or set(value) != {"schema_version", "revision", "config"}
            or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1 or type(value.get("revision")) is not int
            or value["revision"] < 1 or not isinstance(value.get("config"), dict)):
        raise ValueError("Invalid cloud manifest")
    pointer = value["config"]
    if (set(pointer) != {"file_id", "sha256"}
            or not isinstance(pointer.get("file_id"), str)
            or not re.fullmatch(r"[a-zA-Z0-9_-]+", pointer["file_id"])
            or not isinstance(pointer.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", pointer["sha256"])):
        raise ValueError("Invalid cloud manifest config pointer")
    return value


def validate_object(content, manifest, validator, repo_defaults=None):
    if checksum(content) != manifest["config"]["sha256"]:
        raise ValueError("Cloud configuration checksum mismatch")
    value = json.loads(content)
    if (not isinstance(value, dict) or set(value) != {"schema_version", "layers"}
            or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1):
        raise ValueError("Invalid cloud configuration object")
    # Cache original canonical layers, never a resolved effective secret snapshot.
    if canonical_bytes(value) != content:
        raise ValueError("Cloud configuration object must be canonical JSON")
    validate_layers(value, validator, repo_defaults)
    return value


@dataclass(frozen=True)
class PreparedCloudConfig(PreparedConfig):
    store: object
    cloud_object: dict


class GoogleDriveConfigStore(ConfigStore):
    def __init__(self, bootstrap, transport=None, cache_dir=None, secret_provider=None, **kwargs):
        super().__init__(bootstrap, **kwargs)
        drive = bootstrap.get("google_drive", {})
        for key in ("folder_id", "manifest_file_id", "credentials_path"):
            if not isinstance(drive.get(key), str) or not drive[key].strip():
                raise ValueError(f"Google Drive bootstrap requires {key}")
        self.folder_id = drive["folder_id"]
        self.manifest_id = drive["manifest_file_id"]
        self.transport = transport or GoogleDriveTransport(drive["credentials_path"])
        self.cache_dir = Path(cache_dir or Path(get_project_dir()) / "data/cloud-config")
        self.cache_io = LocalConfigStore(self.cache_dir / ".config.yaml")
        self.secrets = secret_provider or LocalSecretStore(bootstrap["node_id"])
        self.soundbank_assets = CloudSoundbankAssets(
            self.transport, self.folder_id, self.cache_dir.parent / "cloud-soundbank/objects"
        )
        self.thread_lock = threading.RLock()
        self.desired_snapshot = None
        self.active_snapshot = None
        self.runtime_source = None
        self.cache_loaded = False
        self.cloud_synced = False
        self.etag = None
        self.last_sync = None
        self.last_error = None
        self.sync_status = "not_synced"
        self.memory_store = None

    def _repo_defaults(self):
        return load_default_config(self.default_path)

    def _resolve(self, obj, repo_defaults=None):
        repo_defaults = self._repo_defaults() if repo_defaults is None else repo_defaults
        return resolve_layers(obj, self.bootstrap["node_id"], repo_defaults)

    def _validate_object(self, content, manifest, repo_defaults=None):
        repo_defaults = self._repo_defaults() if repo_defaults is None else repo_defaults
        return validate_object(content, manifest, self.validator, repo_defaults)

    @contextmanager
    def locked(self):
        self.cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.thread_lock, portalocker.Lock(str(self.cache_dir / ".cloud.lock"), mode="a", timeout=5):
            yield self

    def _envelope(self, manifest, obj, repo_defaults_sha256=None):
        payload = {"manifest_file_id": self.manifest_id, "folder_id": self.folder_id,
                   "node_id": self.bootstrap["node_id"],
                   "manifest": manifest, "object": obj}
        if repo_defaults_sha256 is not None:
            payload["repo_defaults_sha256"] = repo_defaults_sha256
        return {"payload": payload, "sha256": checksum(canonical_bytes(payload))}

    def _read_cache(self, name):
        value = json.loads((self.cache_dir / name).read_bytes())
        payload = value["payload"]
        if (checksum(canonical_bytes(payload)) != value["sha256"]
                or payload["manifest_file_id"] != self.manifest_id
                or payload["folder_id"] != self.folder_id):
            raise ValueError("Cloud cache integrity/source mismatch")
        manifest = parse_manifest(canonical_bytes(payload["manifest"]))
        obj = self._validate_object(canonical_bytes(payload["object"]), manifest)
        # Legacy iteration-1 caches lacked node identity and contain one shared
        # config. Centralized caches must be bound to the locally selected node.
        if payload.get("node_id", self.bootstrap["node_id"] if not centralized(obj) else None) != self.bootstrap["node_id"]:
            raise ValueError("Cloud cache node identity mismatch")
        self._resolve(obj)
        fingerprint = payload.get("repo_defaults_sha256") if name == "active.json" else None
        if fingerprint is not None and (
            not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)
        ):
            raise ValueError("Invalid active LKG defaults fingerprint")
        return self._envelope(manifest, obj, fingerprint)

    def _write_cache(self, name, envelope):
        self.cache_io._atomic_bytes(self.cache_dir / name, canonical_bytes(envelope))

    def _load_cache(self):
        if self.cache_loaded:
            return
        self.cache_loaded = True
        for name, attribute in (("desired.json", "desired_snapshot"), ("active.json", "active_snapshot")):
            try:
                snapshot = self._read_cache(name)
            except (OSError, ValueError, TypeError, KeyError):
                continue
            setattr(self, attribute, snapshot)
            if attribute == "active_snapshot":
                self.active_revision = snapshot["payload"]["manifest"]["revision"]

    def _desired_view(self):
        self._load_cache()
        # Active is only a last-observed desired view for Settings when the
        # desired cache is absent. It never aliases the independent state slots.
        snapshot = self.desired_snapshot or self.active_snapshot
        if snapshot is None:
            raise ConfigUnavailable("No validated desired or active configuration is available")
        return snapshot

    def _cloud_snapshot(self):
        manifest_bytes, etag = self.transport.read_manifest(self.manifest_id)
        manifest = parse_manifest(manifest_bytes)
        content = self.transport.download(manifest["config"]["file_id"])
        obj = self._validate_object(content, manifest)
        self._resolve(obj)
        for previous_snapshot in (self.desired_snapshot, self.active_snapshot):
            if previous_snapshot is None:
                continue
            previous = previous_snapshot["payload"]["manifest"]
            if (manifest["revision"] < previous["revision"] or (
                    manifest["revision"] == previous["revision"]
                    and manifest["config"] != previous["config"])):
                raise ValueError("Cloud revision rollback or revision reuse rejected")
        return self._envelope(manifest, obj), etag

    def refresh_unlocked(self, strict=False):
        self._load_cache()
        self.cloud_synced = False
        self.etag = None
        try:
            snapshot, etag = self._cloud_snapshot()
        except (OSError, ValueError, TypeError, KeyError) as error:
            self.sync_status = "conflict" if isinstance(error, ConfigConflict) else "offline_or_invalid"
            self.conflict = isinstance(error, ConfigConflict)
            self.last_error = f"Cloud sync failed ({type(error).__name__}); runtime fallback requires valid active.json"
            if strict and isinstance(error, ConfigConflict):
                raise
            if strict or (self.desired_snapshot is None and self.active_snapshot is None):
                raise ConfigUnavailable("Cloud configuration unavailable; no usable sync result") from error
        else:
            # A disk failure must not discard the valid in-memory cloud result.
            self.desired_snapshot, self.etag = snapshot, etag
            self.cloud_synced = True
            self.conflict = False
            self.last_sync = datetime.now(timezone.utc).isoformat()
            self.last_error = None
            self.sync_status = "synced"
            try:
                self._write_cache("desired.json", snapshot)
            except OSError:
                self.last_error = "Cloud synced; local desired cache write failed"
                self.sync_status = "cache_error"
        return self.read_unlocked()

    def read_unlocked(self):
        obj = self._desired_view()["payload"]["object"]
        return self._resolve(obj)[1]

    def defaults_unlocked(self):
        obj = self._desired_view()["payload"]["object"]
        return self._resolve(obj)[0]

    def revision_unlocked(self):
        return self._desired_view()["payload"]["manifest"]["revision"]

    def prepare_commit_unlocked(self, base_revision=None):
        super().prepare_commit_unlocked(base_revision)
        self.refresh_unlocked(strict=True)
        if type(base_revision) is not int:
            raise ValueError("Cloud save requires integer base_revision")
        if base_revision != self.revision_unlocked():
            self.conflict = True
            raise ConfigConflict("Cloud revision changed; sync and review edits before saving")

    def commit_unlocked(self, config, base_revision=None):
        # Direct callers prepare once; Settings passes its explicit prepared object.
        self.commit_prepared_unlocked(self.prepare_candidate_unlocked(config), base_revision)

    def commit_prepared_unlocked(self, prepared, base_revision=None):
        if not isinstance(prepared, PreparedCloudConfig) or prepared.store is not self:
            raise TypeError("A cloud configuration prepared by this store is required")
        self.commit_object_unlocked(prepared.cloud_object, base_revision)

    def _persist_secrets(self, pending_secrets):
        try:
            self.secrets.put_many(pending_secrets or {})
        except OSError:
            raise ConfigUnavailable(
                "Node-local secret storage unavailable; cloud configuration was not published"
            ) from None

    def prepare_candidate_unlocked(self, config):
        config, pending_secrets = self.secrets.externalize(config)
        self._persist_secrets(pending_secrets)
        repo_defaults = self._repo_defaults()
        obj = update_node_overrides(
            self._desired_view()["payload"]["object"], self.bootstrap["node_id"], config
        )
        defaults, overrides = self._resolve(obj, repo_defaults)
        effective = merge_configs(defaults, overrides)
        self.validator(effective)
        current_defaults, current_overrides = self._resolve(self._desired_view()["payload"]["object"], repo_defaults)
        published = self.soundbank_assets.publish_layers(
            obj, self.bootstrap["node_id"], repo_defaults, merge_configs(current_defaults, current_overrides)
        )
        defaults, overrides = self._resolve(published, repo_defaults)
        return PreparedCloudConfig(overrides, merge_configs(defaults, overrides), self, published)

    def mutate(self, mutation, base_revision=None):
        """CLI and Settings share validation, immutable upload, verification and CAS."""
        with self.locked():
            super().prepare_commit_unlocked(base_revision)
            self.refresh_unlocked(strict=True)
            old_revision = self.revision_unlocked()
            if base_revision is not None and (type(base_revision) is not int or base_revision != old_revision):
                raise ConfigConflict("Cloud revision changed; read and review before retrying")
            obj = copy.deepcopy(self._desired_view()["payload"]["object"])
            mutation(obj)
            self.commit_object_unlocked(obj, old_revision)
            return old_revision, self.revision_unlocked()

    def commit_object_unlocked(self, obj, base_revision, *, pending_secrets=None):
        # Caller refreshed and captured the revision/ETag before constructing obj.
        if type(base_revision) is not int or base_revision != self.revision_unlocked():
            self.conflict = True
            raise ConfigConflict("Cloud save requires the current base_revision")
        content = canonical_bytes(obj)
        digest = checksum(content)
        manifest = {"schema_version": 1, "revision": base_revision + 1,
                    "config": {"file_id": "pending", "sha256": digest}}
        self._validate_object(content, manifest)
        # This node must remain assigned even in topology administration.
        self._resolve(obj)
        self._persist_secrets(pending_secrets)
        try:
            file_id = self.transport.upload_immutable(
                self.folder_id, content, f"config-{base_revision + 1}-{digest}.json"
            )
            manifest["config"]["file_id"] = file_id
            parse_manifest(canonical_bytes(manifest))
            if checksum(self.transport.download(file_id)) != digest:
                raise ConfigUnavailable("Uploaded cloud object checksum mismatch")
            # Publication happens last. A failed CAS leaves the old manifest intact.
            self.transport.replace_manifest(self.manifest_id, canonical_bytes(manifest), self.etag)
        except ConfigConflict:
            try:
                self.refresh_unlocked(strict=True)
            except (ConfigUnavailable, ConfigConflict):
                pass
            self.conflict = True
            self.sync_status = "conflict"
            self.last_error = "Cloud manifest changed during commit; sync before retrying"
            raise
        except (OSError, ValueError, TypeError, KeyError) as error:
            self.last_error = "Cloud commit failed; sync to confirm desired revision before retrying"
            self.sync_status = "commit_error"
            raise ConfigUnavailable(self.last_error) from error
        self.desired_snapshot = self._envelope(manifest, obj)
        self.conflict = False
        self.last_sync = datetime.now(timezone.utc).isoformat()
        self.sync_status, self.last_error = "synced", None
        try:
            self._write_cache("desired.json", self.desired_snapshot)
        except OSError:
            # Cloud has committed; returning a failed save could encourage retries.
            self.sync_status = "cache_error"
            self.last_error = "Cloud committed; local desired cache write failed"

    def mark_applied(self):
        with self.locked():
            if self.runtime_snapshot is None:
                raise ValueError("No boot configuration has been selected")
            # Publish only the exact boot-selected snapshot after startup succeeds.
            active = copy.deepcopy(self.runtime_snapshot)
            try:
                self._write_cache("active.json", active)
            except OSError:
                self.sync_status = "cache_error"
                self.last_error = "Configuration active; local active snapshot write failed"
            self.active_snapshot = active
            self.active_revision = active["payload"]["manifest"]["revision"]

    def _runtime_config(self, snapshot, repo_defaults):
        """Construct and validate a runtime without publishing or applying it."""
        try:
            payload = snapshot["payload"]
            obj = self._validate_object(canonical_bytes(payload["object"]),
                                        payload["manifest"], repo_defaults)
            defaults, overrides = self._resolve(obj, repo_defaults)
            effective = self.secrets.resolve(merge_configs(defaults, overrides))
            self.validator(effective)
        except (OSError, ValueError, TypeError, KeyError):
            raise ConfigUnavailable("Cloud runtime requires valid node-local secrets and configuration") from None
        return effective

    def source_memory_unlocked(self):
        # The committed hot snapshot remains authoritative even after a
        # post-CAS YAML persistence failure. Do not refresh or materialize here.
        return self.memory_store.scopes() if self.memory_store is not None else None

    def validate_runtime_readiness(self, source_memory=None):
        """Preflight requires live Drive and local secrets; it never selects a boot."""
        with self.locked():
            self.refresh_unlocked(strict=True)
            effective = self._runtime_config(self.desired_snapshot, self._repo_defaults())
            try:
                memory = self._memory_backend(effective)
                if memory is not None:
                    memory.preflight(source_memory=source_memory)
                else:
                    require_memory_match(source_memory, None)
            except MemoryConflict:
                raise ConfigConflict(MemoryConflict.message) from None
            except MemoryStorageError as error:
                raise ConfigUnavailable(str(error)) from None

    def _memory_backend(self, effective):
        provider = explicit_memory_config(effective)
        if provider is None:
            return None
        return CloudMemoryStore(self.bootstrap, self.transport,
                                self.cache_dir.parent / "cloud-memory", provider)

    def verify_provisioning_readiness(self, source_memory):
        """Live verification without cache writes, materialization or boot selection."""
        with self.thread_lock:
            self._load_cache()
            snapshot, _ = self._cloud_snapshot()
            effective = self._runtime_config(snapshot, self._repo_defaults())
            self.soundbank_assets.verify_remote(effective, compare_local=True)
            memory = self._memory_backend(effective)
            if memory is None:
                require_memory_match(source_memory, None)
                memory_revision = None
            else:
                payload = memory.preflight(source_memory=source_memory)
                if payload["manifest"]["writer_node_id"] != self.bootstrap["node_id"]:
                    raise ConfigUnavailable("Cloud Memory writer differs; provisioning cannot claim readiness")
                memory_revision = payload["manifest"]["revision"]
            return snapshot["payload"]["manifest"]["revision"], memory_revision, effective

    def memory_storage(self, provider_config):
        if (self.memory_store is None or self.runtime_snapshot is None
                or memory_path(provider_config) != self.memory_store.path):
            raise MemoryUnavailable()
        return self.memory_store

    def prepare_runtime(self):
        with self.locked():
            self.runtime_snapshot = None
            self.runtime_source = None
            self.memory_store = None
            self.refresh_unlocked()
            if self.cloud_synced:
                snapshot = self.desired_snapshot
                source = "drive"
            else:
                # desired.json may contain a saved revision that never ran.
                # Only a confirmed active snapshot is an offline runtime LKG.
                snapshot = self.active_snapshot
                source = "active_lkg"
            if snapshot is None:
                raise ConfigUnavailable("Drive unavailable or invalid and no valid active.json runtime LKG exists")
            # Capture one parsed release baseline for both resolution and its
            # fingerprint, before any secret resolution. Never hash runtime secrets.
            try:
                repo_defaults = self._repo_defaults()
                fingerprint = checksum(canonical_bytes(repo_defaults))
            except (OSError, ValueError, TypeError, KeyError):
                raise ConfigUnavailable("Cannot validate software defaults for cloud runtime") from None
            if source == "active_lkg" and snapshot["payload"].get("repo_defaults_sha256") != fingerprint:
                raise ConfigUnavailable("Active LKG is incompatible with current repo defaults; successful online startup required")
            effective = self._runtime_config(snapshot, repo_defaults)
            try:
                memory = self._memory_backend(effective)
                if memory is not None:
                    memory.sync()
            except MemoryConflict:
                raise ConfigConflict(MemoryConflict.message) from None
            except MemoryStorageError as error:
                raise ConfigUnavailable(str(error)) from None
            if source == "drive" and self.active_snapshot is not None:
                active_defaults, active_overrides = self._resolve(self.active_snapshot["payload"]["object"], repo_defaults)
                self.soundbank_assets.retain(merge_configs(active_defaults, active_overrides))
            self.soundbank_assets.materialize(effective)
            self.runtime_snapshot = self._envelope(
                snapshot["payload"]["manifest"], snapshot["payload"]["object"], fingerprint
            )
            self.runtime_snapshot = copy.deepcopy(self.runtime_snapshot)
            self.runtime_source = source
            self.memory_store = memory
            return effective

    def status_unlocked(self):
        result = super().status_unlocked()
        assignment = node_assignment(self._desired_view()["payload"]["object"], self.bootstrap["node_id"])
        active_assignment = (node_assignment(self.active_snapshot["payload"]["object"], self.bootstrap["node_id"])
                             if self.active_snapshot else {"environment": None, "role": None})
        result.update(
            environment=assignment["environment"], role=assignment["role"],
            active_environment=active_assignment["environment"], active_role=active_assignment["role"],
            runtime_revision=(self.runtime_snapshot["payload"]["manifest"]["revision"]
                              if self.runtime_snapshot else None),
            runtime_source=self.runtime_source,
            desired_snapshot_source=("drive" if self.cloud_synced else
                                     "desired_cache" if self.desired_snapshot else "active_only"),
            source=f"Google Drive manifest: {self.manifest_id}",
            active_source="data/cloud-config/active.json",
            cache_path="data/cloud-config/desired.json", sync_status=self.sync_status,
            last_sync=self.last_sync, last_error=self.last_error,
        )
        return result
