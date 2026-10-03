"""Explicit Local -> existing Cloud State provisioning; never select a provider."""

import copy
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

import portalocker

from config.bootstrap import load_bootstrap, save_bootstrap
from config.cloud_layers import centralized, update_node_overrides
from config.cloud_memory import CloudMemoryStore, MemorySchema, parse_manifest as parse_memory_manifest
from config.cloud_secrets import LocalSecretStore, REFERENCE, SecretProvider
from config.config_loader import get_project_dir, merge_configs
from config.config_store import ConfigConflict, LocalConfigStoreAdapter, canonical_bytes, checksum
from config.drive_transport import GoogleDriveTransport
from config.google_drive_config import GoogleDriveConfigStore
from config.local_config import LocalConfigStore
from config.memory_reconciliation import explicit_memory_config, read_local_memory
from core.memory_storage import MemoryReconciliationRequired
from core.utils.config_secrets import is_secret_name


class ProvisioningError(Exception):
    message = "Provisioning failed; verify Local state and Drive access. Provider remains Local; remote state may already be committed."

    def __init__(self):
        super().__init__(self.message)


class ProvisioningConflict(ProvisioningError):
    message = "Provisioning conflict; refresh and review remote state before rerunning. Provider remains Local."


class ProvisioningReconciliation(ProvisioningError):
    message = "Provisioning requires explicit memory/configuration reconciliation. Provider remains Local."


class ProvisioningWriterMismatch(ProvisioningError):
    message = "Cloud Memory has another writer; provisioning cannot transfer writer authority. Provider remains Local."


class ProvisioningRecoveryRequired(ProvisioningReconciliation):
    message = "Memory manifest creation was interrupted; recover its remote ID in provisioning metadata before rerunning. Provider remains Local."


class BootstrapPublicationError(ProvisioningError):
    message = "Remote state verified, but bootstrap metadata publication failed. Provider remains Local; rerun provisioning to recover using the saved receipt."


@dataclass(frozen=True)
class ProvisioningResult:
    config_revision: int
    memory_revision: int | None
    memory_reused: bool
    descriptor_file_id: str | None = None


def semantic_config(config):
    """Only dependent storage pointers and string-entry representation are neutral."""
    result = copy.deepcopy(config)
    entries = result.get("static_soundbank", {}).get("entries", {})
    for phrase, entry in list(entries.items()):
        if isinstance(entry, str):
            entries[phrase] = {"file": entry}
        elif isinstance(entry, dict):
            entry.pop("cloud", None)
            if isinstance(entry.get("optimized"), dict):
                entry["optimized"].pop("cloud", None)
    return result


class _PreviewSecrets(SecretProvider):
    def __init__(self, provider, pending=None, *, comparison=False):
        self.provider = provider
        self.pending = pending or {}
        self.comparison = comparison

    def get(self, name):
        if name in self.pending:
            return self.pending[name]
        try:
            return self.provider.get(name)
        except ValueError:
            if self.comparison:
                return object()  # Unknown refs compare different, never become published values.
            raise

    def put_many(self, values):
        raise AssertionError("Preflight cannot persist secrets")


def _equal(left, right):
    try:
        return canonical_bytes(left) == canonical_bytes(right)
    except (TypeError, ValueError):
        return False


def _delta(desired, baseline):
    if isinstance(desired, dict) and isinstance(baseline, dict):
        if set(baseline) - set(desired):
            # Recursive configuration merges cannot delete inherited keys.
            raise ProvisioningReconciliation()
        patch = {}
        for key, value in desired.items():
            if key not in baseline:
                patch[key] = copy.deepcopy(value)
            elif not _equal(value, baseline[key]):
                patch[key] = _delta(value, baseline[key])
        return patch
    return copy.deepcopy(desired)


def _reuse_references(value, previous, secrets, *, secret=False):
    if secret:
        match = REFERENCE.fullmatch(previous) if isinstance(previous, str) else None
        if match:
            try:
                if _equal(value, secrets.get(match[1])):
                    return previous
            except ValueError:
                pass
        return copy.deepcopy(value)
    if isinstance(value, dict):
        previous = previous if isinstance(previous, dict) else {}
        return {key: _reuse_references(child, previous.get(key), secrets, secret=is_secret_name(key))
                for key, child in value.items()}
    if isinstance(value, list):
        previous = previous if isinstance(previous, list) else []
        return [_reuse_references(child, previous[index] if index < len(previous) else None, secrets)
                for index, child in enumerate(value)]
    return copy.deepcopy(value)


class _Receipt:
    """Reference-only recovery for manifest creation/bootstrap publication gaps."""
    def __init__(self, path, bootstrap):
        self.path = Path(path)
        drive = bootstrap["google_drive"]
        self.identity = {"schema_version": 1, "node_id": bootstrap["node_id"],
                         "folder_id": drive["folder_id"], "manifest_file_id": drive["manifest_file_id"]}

    def read(self):
        if not self.path.exists():
            return None
        if self.path.is_symlink():
            raise ProvisioningError()
        value = json.loads(self.path.read_bytes())
        payload = value["payload"]
        if (set(value) != {"payload", "sha256"}
                or set(payload) != {*self.identity, "memory_manifest_file_id"}
                or any(payload[key] != item for key, item in self.identity.items())
                or value["sha256"] != checksum(canonical_bytes(payload))):
            raise ProvisioningError()
        if payload["memory_manifest_file_id"] is None:
            raise ProvisioningRecoveryRequired()
        return GoogleDriveTransport._id(payload["memory_manifest_file_id"])

    def _persist(self, manifest_id):
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise ProvisioningError()
        payload = {**self.identity, "memory_manifest_file_id": manifest_id}
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        envelope = {"payload": payload, "sha256": checksum(canonical_bytes(payload))}
        LocalConfigStore(self.path.parent / ".unused.yaml")._atomic_bytes(self.path, canonical_bytes(envelope))

    def begin(self):
        # A lost upload response or failed ID write must not allow a new authority.
        self._persist(None)

    def write(self, manifest_id):
        self._persist(GoogleDriveTransport._id(manifest_id))


def _existing_memory(bootstrap, transport, cache_dir, provider, snapshot):
    backend = CloudMemoryStore(bootstrap, transport, cache_dir, provider)
    payload = backend.preflight(source_memory=snapshot["scopes"])
    if payload["manifest"]["writer_node_id"] != bootstrap["node_id"]:
        raise ProvisioningWriterMismatch()
    if not _equal(payload["snapshot"], snapshot):
        raise ProvisioningReconciliation()
    return payload["manifest"]["revision"]


def _seed_memory(bootstrap, transport, provider, snapshot, receipt):
    content = canonical_bytes(snapshot)
    manifest = {"schema_version": 1, "revision": 1, "writer_node_id": bootstrap["node_id"],
                "snapshot": {"file_id": "pending", "sha256": checksum(content)}}
    folder = bootstrap["google_drive"]["folder_id"]
    manifest["snapshot"]["file_id"] = transport.upload_immutable(
        folder, content, f"memory-1-{checksum(content)}.json"
    )
    parse_memory_manifest(canonical_bytes(manifest))
    MemorySchema(provider).validate_object(transport.download(manifest["snapshot"]["file_id"]), manifest)
    manifest_bytes = canonical_bytes(manifest)
    receipt.begin()
    manifest_id = transport.upload_immutable(folder, manifest_bytes, "memory-manifest.json")
    # Persist a known returned ID before verification/bootstrap: reruns must
    # inspect this authority, never silently create a second one.
    receipt.write(manifest_id)
    if transport.download(manifest_id) != manifest_bytes:
        raise ProvisioningError()
    parse_memory_manifest(manifest_bytes)
    return manifest_id


def provision_cloud_state(local_store, *, bootstrap_path=None, transport=None, secret_provider=None,
                          receipt_path=None, runtime_cache_dir=None, recovery_passphrase=None,
                          source_label=None, rotate_recovery_passphrase=False):
    """Provision a pre-existing Config source. Returns only safe status metadata."""
    try:
        if not isinstance(local_store, LocalConfigStoreAdapter) or local_store.bootstrap["config_provider"] != "local":
            raise ProvisioningError()
        original_bootstrap = load_bootstrap(bootstrap_path)
        if (original_bootstrap["config_provider"] != "local"
                or original_bootstrap["node_id"] != local_store.bootstrap["node_id"]):
            raise ProvisioningError()
        drive = original_bootstrap["google_drive"]
        for key in ("folder_id", "manifest_file_id"):
            GoogleDriveTransport._id(drive[key])
        cloud_bootstrap = {**copy.deepcopy(original_bootstrap), "config_provider": "google_drive"}
        secrets = secret_provider or LocalSecretStore(original_bootstrap["node_id"])
        transport = transport or GoogleDriveTransport(drive["credentials_path"])
        runtime_cache_dir = Path(runtime_cache_dir or Path(get_project_dir()) / "data/cloud-config")
        identity = checksum(canonical_bytes({"node_id": original_bootstrap["node_id"],
                                             "folder_id": drive["folder_id"],
                                             "manifest_file_id": drive["manifest_file_id"]}))
        receipt = _Receipt(receipt_path or Path(get_project_dir()) / f"data/cloud-provisioning/{identity}.json",
                           original_bootstrap)
        receipt.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with portalocker.Lock(str(receipt.path.parent / ".provision.lock"), mode="a", timeout=5), \
                local_store.locked(), tempfile.TemporaryDirectory(prefix="xiaozhi-provision-") as scratch:
            layers = local_store.source_layers_unlocked()
            effective_local = merge_configs(layers["defaults"], layers["overrides"])
            local_store.validator(effective_local)
            source_semantics = secrets.resolve(semantic_config(effective_local))
            canonical_bytes(source_semantics)
            memory_provider = explicit_memory_config(effective_local)
            scopes = read_local_memory(memory_provider)
            snapshot = {"schema_version": 1, "scopes": scopes} if memory_provider is not None else None
            if snapshot is not None:
                MemorySchema(memory_provider).validate(snapshot)

            readonly = GoogleDriveConfigStore(cloud_bootstrap, transport=transport, secret_provider=secrets,
                                             cache_dir=runtime_cache_dir, default_path=local_store.default_path)
            cloud = GoogleDriveConfigStore(cloud_bootstrap, transport=transport, secret_provider=secrets,
                                          cache_dir=Path(scratch) / "cloud-config", default_path=local_store.default_path)
            # Preserve existing revision floors without changing deployment caches.
            readonly._load_cache()
            cloud.desired_snapshot = copy.deepcopy(readonly.desired_snapshot)
            cloud.active_snapshot = copy.deepcopy(readonly.active_snapshot)
            cloud.cache_loaded = True
            cloud.soundbank_assets.validate_local(effective_local)
            transport.validate_folder(drive["folder_id"])
            with cloud.locked():
                cloud.refresh_unlocked(strict=True)
                revision = cloud.revision_unlocked()
                current_obj = cloud._desired_view()["payload"]["object"]
                if not centralized(current_obj):
                    raise ProvisioningReconciliation()  # No topology guessing for legacy per-manifest configs.
                baseline, _ = cloud._resolve(update_node_overrides(current_obj, original_bootstrap["node_id"], {}))
                current_defaults, current_overrides = cloud._resolve(current_obj)
                current_effective = merge_configs(current_defaults, current_overrides)
                local_overrides = secrets.resolve(semantic_config(layers["overrides"]))
                comparison = _PreviewSecrets(secrets, comparison=True).resolve(semantic_config(baseline))
                patch = merge_configs(local_overrides, _delta(source_semantics, merge_configs(comparison, local_overrides)))
                patch = _reuse_references(patch, current_effective, secrets)
                proposed = update_node_overrides(current_obj, original_bootstrap["node_id"], patch)
                patch = cloud.soundbank_assets.provisioning_overrides(proposed, original_bootstrap["node_id"], cloud._repo_defaults())

                # Pure secret externalization and full all-node validation before any upload.
                preview, pending = secrets.externalize(patch)
                preview_obj = update_node_overrides(current_obj, original_bootstrap["node_id"], preview)
                content = canonical_bytes(preview_obj)
                preview_manifest = {"schema_version": 1, "revision": revision + 1,
                                    "config": {"file_id": "pending", "sha256": checksum(content)}}
                cloud._validate_object(content, preview_manifest)
                defaults, overrides = cloud._resolve(preview_obj)
                preview_effective = merge_configs(defaults, overrides)
                resolved = _PreviewSecrets(secrets, pending).resolve(preview_effective)
                cloud.validator(resolved)
                if not _equal(semantic_config(resolved), source_semantics):
                    raise ProvisioningReconciliation()
                cloud.soundbank_assets.validate_local(preview_effective, current_effective)

                memory_id = drive.get("memory_manifest_file_id")
                if memory_provider is not None:
                    recovered_id = receipt.read()
                    if memory_id is not None and recovered_id is not None and memory_id != recovered_id:
                        raise ProvisioningReconciliation()
                    memory_id = memory_id if memory_id is not None else recovered_id
                    if memory_id is not None:
                        cloud_bootstrap["google_drive"]["memory_manifest_file_id"] = memory_id
                        _existing_memory(cloud_bootstrap, transport, runtime_cache_dir.parent / "cloud-memory",
                                         memory_provider, snapshot)

                # Reuse Settings' one candidate preparation and CAS-last commit.
                prepared = cloud.prepare_candidate_unlocked(patch)
                if not _equal(prepared.cloud_object, current_obj):
                    cloud.commit_prepared_unlocked(prepared, revision)

            memory_reused = memory_id is not None and memory_provider is not None
            if memory_provider is not None and memory_id is None:
                memory_id = _seed_memory(cloud_bootstrap, transport, memory_provider, snapshot, receipt)
                cloud_bootstrap["google_drive"]["memory_manifest_file_id"] = memory_id

            # Validate the final live state, not merely the prepared candidate.
            readonly = GoogleDriveConfigStore(cloud_bootstrap, transport=transport, secret_provider=secrets,
                                             cache_dir=runtime_cache_dir, default_path=local_store.default_path)
            config_revision, memory_revision, final_effective = readonly.verify_provisioning_readiness(scopes)
            if (not _equal(semantic_config(final_effective), source_semantics)
                    or not _equal(local_store.source_layers_unlocked(), layers)
                    or not _equal(read_local_memory(memory_provider), scopes)
                    or load_bootstrap(bootstrap_path) != original_bootstrap):
                raise ProvisioningConflict()
            if memory_provider is not None:
                _existing_memory(cloud_bootstrap, transport, runtime_cache_dir.parent / "cloud-memory",
                                 memory_provider, snapshot)
            final_bootstrap = {**cloud_bootstrap, "config_provider": "local"}
            descriptor_id = None
            if recovery_passphrase is not None:
                from config.cloud_recovery import RecoveryConflict, authority_source_id, publish_recovery_descriptor
                recovery_bootstrap = copy.deepcopy(final_bootstrap)
                if memory_revision is None:
                    recovery_bootstrap["google_drive"].pop("memory_manifest_file_id", None)
                try:
                    descriptor_id = publish_recovery_descriptor(recovery_bootstrap, transport, secrets,
                        recovery_passphrase, label=source_label, rotate=rotate_recovery_passphrase,
                        default_path=local_store.default_path, cache_dir=runtime_cache_dir,
                        creation_receipt=receipt.path.parent / (authority_source_id(drive["folder_id"], drive["manifest_file_id"])
                                                               + ".descriptor.json"))
                except RecoveryConflict:
                    raise ProvisioningConflict() from None
            try:
                save_bootstrap(final_bootstrap, bootstrap_path)
            except Exception:
                raise BootstrapPublicationError() from None
            return ProvisioningResult(config_revision, memory_revision, memory_reused, descriptor_id)
    except ProvisioningError:
        raise
    except ConfigConflict:
        raise ProvisioningConflict() from None
    except MemoryReconciliationRequired:
        raise ProvisioningReconciliation() from None
    except Exception:
        raise ProvisioningError() from None
