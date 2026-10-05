"""Journaled node creation across Config and recovery's separate CAS authorities."""

import copy
import re
from pathlib import Path

import portalocker

from config.cloud_layers import centralized, legacy_centralized
from config.cloud_recovery import (
    DiscoveredSource, MemorySecrets, RecoveryConflict, RecoveryError,
    RecoveryIdentityConflict, decrypt_dataset, encrypt_dataset, fetch_backup,
    parse_json, validate_descriptor, validate_pointer,
)
from config.config_loader import get_project_dir
from config.config_store import ConfigConflict, canonical_bytes, checksum
from config.local_config import LocalConfigStore
from config.recovery_oauth import credential_payload


class CloneNodeConflict(RecoveryConflict):
    message = "New node ID already exists or disagrees with this clone transaction; choose a different ID."


class ClonePublicationError(RecoveryError):
    message = "Node clone interrupted; retry the same source, backup node and new node ID to resume."


class CloneUnsupported(RecoveryError):
    message = "Clone requires current centralized Cloud Config; legacy sources support existing-node restore only."


def validate_new_node_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,191}", value):
        raise RecoveryError()
    return value


def _identity(source, source_node, new_node):
    descriptor = validate_descriptor(source.descriptor)
    if source_node not in descriptor["nodes"]:
        raise RecoveryError()
    return {key: descriptor[key] for key in (
        "source_id", "folder_id", "config_manifest_file_id", "memory_manifest_file_id"
    )} | {"descriptor_file_id": source.file_id, "source_node_id": source_node,
         "new_node_id": validate_new_node_id(new_node)}


class _CloneJournal:
    """Only ciphertext and reference-only assignment are staged; never a password.

    Config's exact proposed manifest is durable BEFORE CAS. It proves a lost CAS
    response on retry. A verified receipt allows later unrelated Config saves.
    The descriptor entry has its own unique immutable encrypted-backup pointer.
    Completed intents remain as private receipts, including across local retry.
    """
    def __init__(self, data):
        self.directory = data / ".cloud-clone"
        self.path = self.directory / "journal.json"
        self.encrypted_path = self.directory / "secrets.enc.json"

    def read(self):
        if self.directory.is_symlink() or self.path.is_symlink() or self.encrypted_path.is_symlink():
            raise RecoveryIdentityConflict()
        if not self.path.exists():
            return None
        value = parse_json(self.path.read_bytes())
        if (not isinstance(value, dict) or set(value) != {"schema_version", "identity", "assignment",
                "encrypted_sha256", "backup", "config_attempt", "config_published"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or not isinstance(value["identity"], dict)
                or set(value["identity"]) != {"source_id", "folder_id", "config_manifest_file_id",
                    "memory_manifest_file_id", "descriptor_file_id", "source_node_id", "new_node_id"}
                or not isinstance(value["assignment"], dict)
                or set(value["assignment"]) != {"environment", "role", "overrides"}
                or type(value["config_published"]) is not bool
                or checksum(self.encrypted_path.read_bytes()) != value["encrypted_sha256"]):
            raise RecoveryIdentityConflict()
        validate_new_node_id(value["identity"]["new_node_id"])
        if value["backup"] is not None:
            validate_pointer(value["backup"])
            if value["backup"]["sha256"] != value["encrypted_sha256"]:
                raise RecoveryIdentityConflict()
        if value["config_attempt"] is not None:
            attempt = value["config_attempt"]
            if (not isinstance(attempt, dict) or set(attempt) != {"schema_version", "revision", "config"}
                    or type(attempt["schema_version"]) is not int or attempt["schema_version"] != 1
                    or type(attempt["revision"]) is not int or attempt["revision"] < 1):
                raise RecoveryIdentityConflict()
            validate_pointer(attempt["config"])
        if value["config_published"] and value["config_attempt"] is None:
            raise RecoveryIdentityConflict()
        return value

    def write(self, value):
        from config.cloud_restore import _atomic
        _atomic(self.path, canonical_bytes(value))
        # Atomic file writes sync the journal directory. Sync its parent too so
        # the newly created directory entry survives before either remote CAS.
        LocalConfigStore._sync_directory(self.directory.parent)

    def create(self, identity, assignment, encrypted):
        from config.cloud_restore import _atomic
        _atomic(self.encrypted_path, encrypted)
        value = {"schema_version": 1, "identity": identity, "assignment": assignment,
                 "encrypted_sha256": checksum(encrypted), "backup": None,
                 "config_attempt": None, "config_published": False}
        self.write(value)
        return value


def clone_selection(data_dir):
    """Safe UI resume metadata; contains no pointers, secrets or filesystem paths."""
    try:
        value = _CloneJournal(Path(data_dir)).read()
        return None if value is None else {key: value["identity"][key]
            for key in ("source_id", "source_node_id", "new_node_id")}
    except RecoveryError:
        raise
    except Exception:
        raise ClonePublicationError() from None


def _bootstrap(identity, data, node):
    result = {"config_provider": "google_drive", "node_id": node, "google_drive": {
        "folder_id": identity["folder_id"], "manifest_file_id": identity["config_manifest_file_id"],
        "credentials_path": str(data / "drive-credentials.json")}}
    if identity["memory_manifest_file_id"] is not None:
        result["google_drive"]["memory_manifest_file_id"] = identity["memory_manifest_file_id"]
    return result


def _descriptor(transport, identity):
    content, etag = transport.read_manifest(identity["descriptor_file_id"])
    descriptor = validate_descriptor(parse_json(content))
    if any(descriptor[key] != identity[key] for key in (
        "source_id", "folder_id", "config_manifest_file_id", "memory_manifest_file_id"
    )) or identity["source_node_id"] not in descriptor["nodes"]:
        raise RecoveryConflict()
    return descriptor, etag


def _config(store):
    snapshot, etag = store._cloud_snapshot()
    payload = snapshot["payload"]
    obj = payload["object"]
    if not centralized(obj) or legacy_centralized(obj):
        raise CloneUnsupported()
    return payload["manifest"], obj, etag


def _preflight(store, obj, assignment, node):
    candidate = copy.deepcopy(obj)
    candidate["layers"]["nodes"][node] = copy.deepcopy(assignment)
    content = canonical_bytes(candidate)
    manifest = {"schema_version": 1, "revision": 1,
                "config": {"file_id": "preflight", "sha256": checksum(content)}}
    store._validate_object(content, manifest)
    effective = store._runtime_config(store._envelope(manifest, candidate), store._repo_defaults())
    store.soundbank_assets.verify_remote(effective)
    memory = store._memory_backend(effective)
    if memory is not None:
        memory.preflight()  # Read-only: a clone never claims Memory writer ownership.


def _accept_config(value, manifest, obj):
    node = value["identity"]["new_node_id"]
    assignment = obj["layers"]["nodes"].get(node)
    if assignment is None:
        if value["config_published"]:
            raise CloneNodeConflict()
        return False
    attempt = value["config_attempt"]
    if assignment != value["assignment"] or attempt is None:
        raise CloneNodeConflict()
    if not value["config_published"] and manifest != attempt:
        # Equal assignments alone cannot prove ownership of someone else's CAS.
        raise CloneNodeConflict()
    if (manifest["revision"] < attempt["revision"] or
            manifest["revision"] == attempt["revision"] and manifest != attempt):
        raise RecoveryConflict()
    return True


def _publish_config(journal, value, store, target_store):
    manifest, obj, etag = _config(store)
    if not _accept_config(value, manifest, obj):
        if value["config_attempt"] and manifest["revision"] < value["config_attempt"]["revision"] - 1:
            raise RecoveryConflict()
        candidate = copy.deepcopy(obj)
        candidate["layers"]["nodes"][value["identity"]["new_node_id"]] = copy.deepcopy(value["assignment"])
        _preflight(target_store, obj, value["assignment"], value["identity"]["new_node_id"])
        content = canonical_bytes(candidate)
        file_id = store.transport.upload_immutable(store.folder_id, content, "cloned-node-config.json")
        if store.transport.download(file_id) != content:
            raise RecoveryError()
        proposed = {"schema_version": 1, "revision": manifest["revision"] + 1,
                    "config": {"file_id": file_id, "sha256": checksum(content)}}
        value["config_attempt"] = proposed
        journal.write(value)
        store.transport.replace_manifest(store.manifest_id, canonical_bytes(proposed), etag)
        current, current_obj, _ = _config(store)
        _accept_config(value, current, current_obj)
    value["config_published"] = True
    journal.write(value)


def _publish_descriptor(journal, value, transport):
    identity = value["identity"]
    descriptor, etag = _descriptor(transport, identity)
    node = identity["new_node_id"]
    entry = {"secrets": value["backup"]}
    if node in descriptor["nodes"]:
        if descriptor["nodes"][node] != entry:
            raise CloneNodeConflict()
    else:
        descriptor["nodes"][node] = entry
        transport.replace_manifest(identity["descriptor_file_id"],
                                   canonical_bytes(validate_descriptor(descriptor)), etag)


def clone_cloud_node(source, source_node_id, new_node_id, passphrase, credential_bytes, transport, *,
                     data_dir=None, default_path=None):
    """Create both remote bindings, verify, then delegate NEW-ID local recovery.

    No compensating delete/rollback or remote GC. Retrying the same intent merges
    only the missing addition into the latest authority with a fresh CAS.
    """
    try:
        # Status/resume metadata must stay free of audio/runtime imports.
        from config.cloud_restore import _RestoreJournal, _check_existing, _private_directory, restore_cloud_node
        from config.google_drive_config import GoogleDriveConfigStore
        identity = _identity(source, source_node_id, new_node_id)
        credential_bytes = canonical_bytes(credential_payload(credential_bytes))
        data = Path(data_dir or Path(get_project_dir()) / "data")
        _private_directory(data)
        LocalConfigStore._sync_directory(data.parent)
        with portalocker.Lock(str(data / ".cloud-clone.lock"), mode="a", timeout=5):
            journal = _CloneJournal(data)
            value = journal.read()
            if value is not None and value["identity"] != identity:
                raise RecoveryIdentityConflict()
            descriptor, descriptor_etag = _descriptor(transport, identity)
            if value is None:
                if descriptor_etag != source.etag or descriptor != source.descriptor:
                    raise RecoveryConflict()
                if new_node_id in descriptor["nodes"]:
                    raise CloneNodeConflict()
                source_dataset = fetch_backup(transport, descriptor["nodes"][source_node_id]["secrets"],
                                              identity["source_id"], source_node_id, passphrase)
                dataset = {"node_id": new_node_id, "values": copy.deepcopy(source_dataset["values"])}
            else:
                dataset = decrypt_dataset(journal.encrypted_path.read_bytes(), identity["source_id"], new_node_id, passphrase)
                existing = descriptor["nodes"].get(new_node_id)
                if existing is not None and existing != {"secrets": value["backup"]}:
                    raise CloneNodeConflict()
            bootstrap = _bootstrap(identity, data, new_node_id)
            _check_existing(data, bootstrap, dataset)
            local_intent = _RestoreJournal(data).pending()
            if local_intent and (local_intent["source_id"] != identity["source_id"] or local_intent["node_id"] != new_node_id):
                raise RecoveryIdentityConflict()
            options = {"transport": transport, "secret_provider": MemorySecrets(dataset),
                       "cache_dir": data / "cloud-config", "default_path": default_path}
            store = GoogleDriveConfigStore(_bootstrap(identity, data, source_node_id), **options)
            target_store = GoogleDriveConfigStore(bootstrap, **options)
            manifest, obj, _ = _config(store)
            if value is None:
                if new_node_id in obj["layers"]["nodes"]:
                    raise CloneNodeConflict()
                # V2 cluster state is inherited from the same authority. Copy
                # only the assignment and explicit exceptions, never a resolved
                # runtime snapshot into the new node's overrides.
                assignment = copy.deepcopy(obj["layers"]["nodes"][source_node_id])
                _preflight(target_store, obj, assignment, new_node_id)
                encrypted = encrypt_dataset(dataset, identity["source_id"], new_node_id, passphrase)
                value = journal.create(identity, assignment, encrypted)
            else:
                _accept_config(value, manifest, obj)
                _preflight(target_store, obj, value["assignment"], new_node_id)
            if value["backup"] is None:
                encrypted = journal.encrypted_path.read_bytes()
                file_id = transport.upload_immutable(identity["folder_id"], encrypted, "cloned-node-secrets.enc.json")
                value["backup"] = {"file_id": file_id, "sha256": checksum(encrypted)}
                if fetch_backup(transport, value["backup"], identity["source_id"], new_node_id, passphrase) != dataset:
                    raise RecoveryError()
                journal.write(value)
            else:
                if fetch_backup(transport, value["backup"], identity["source_id"], new_node_id, passphrase) != dataset:
                    raise RecoveryError()
            _publish_config(journal, value, store, target_store)
            _publish_descriptor(journal, value, transport)
            # Verify both live authorities before any deployed local identity.
            final_manifest, final_obj, _ = _config(store)
            _accept_config(value, final_manifest, final_obj)
            final_descriptor, final_etag = _descriptor(transport, identity)
            if final_descriptor["nodes"].get(new_node_id) != {"secrets": value["backup"]}:
                raise RecoveryConflict()
            verified = DiscoveredSource(source.file_id, final_etag, final_descriptor)
            return restore_cloud_node(verified, new_node_id, passphrase, credential_bytes, transport,
                                      data_dir=data, default_path=default_path, activate=True)
    except (RecoveryConflict, RecoveryIdentityConflict, CloneUnsupported):
        raise
    except ConfigConflict:
        raise RecoveryConflict() from None
    except RecoveryError:
        raise
    except Exception:
        raise ClonePublicationError() from None
