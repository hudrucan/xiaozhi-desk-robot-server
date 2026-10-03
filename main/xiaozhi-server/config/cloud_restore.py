"""Read-only Cloud validation followed by journaled local identity publication."""

import copy
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

import portalocker
import yaml

from config.bootstrap import load_bootstrap
from config.cloud_recovery import (
    MemorySecrets, RecoveryConflict, RecoveryError, RecoveryIdentityConflict,
    fetch_backup, parse_json, validate_dataset, validate_descriptor,
)
from config.config_loader import get_project_dir
from config.config_store import ConfigConflict, canonical_bytes, checksum
from config.google_drive_config import GoogleDriveConfigStore
from config.local_config import LocalConfigStore
from config.recovery_oauth import credential_payload


class RestorePublicationError(RecoveryError):
    message = "Local recovery publication interrupted; rerun restore for the same source/node to resume the private journal."


def select_source(sources, source_id=None):
    matches = [item for item in sources if source_id is None or item.descriptor["source_id"] == source_id]
    if len(matches) != 1:
        raise RecoveryError()
    return matches[0]


def select_node(source, node_id=None):
    nodes = source.descriptor["nodes"]
    if node_id is not None and node_id in nodes:
        return node_id
    if node_id is None and len(nodes) == 1:
        return next(iter(nodes))
    raise RecoveryError()


def _private_directory(path):
    if path.is_symlink():
        raise RecoveryIdentityConflict()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _atomic(path, content):
    if path.is_symlink():
        raise RecoveryIdentityConflict()
    _private_directory(path.parent)
    LocalConfigStore(path.parent / ".unused.yaml")._atomic_bytes(path, content)
    os.chmod(path, 0o600)


def _check_existing(data, bootstrap, dataset):
    if data.is_symlink() or (data / "node-secrets").is_symlink():
        raise RecoveryIdentityConflict()
    path = data / "bootstrap.yaml"
    if path.is_symlink():
        raise RecoveryIdentityConflict()
    if path.exists():
        current = load_bootstrap(path)
        expected, actual = bootstrap["google_drive"], current.get("google_drive", {})
        if current["node_id"] != bootstrap["node_id"] or any(
            actual.get(key) != expected.get(key) for key in ("folder_id", "manifest_file_id")
        ) or (actual.get("memory_manifest_file_id") is not None and expected.get("memory_manifest_file_id") is not None
              and actual["memory_manifest_file_id"] != expected["memory_manifest_file_id"]):
            raise RecoveryIdentityConflict()
    secret_dir = data / "node-secrets"
    expected_name = hashlib.sha256(bootstrap["node_id"].encode("utf-8")).hexdigest() + ".json"
    if secret_dir.exists():
        for item in secret_dir.glob("*.json"):
            if item.is_symlink() or item.name != expected_name:
                raise RecoveryIdentityConflict()
            existing = validate_dataset(parse_json(item.read_bytes()), bootstrap["node_id"])
            if canonical_bytes(existing) != canonical_bytes(dataset):
                raise RecoveryIdentityConflict()


class _RestoreJournal:
    """Bootstrap last: interruption leaves staged data and a resumable intent.

    Staged credentials/secrets are private plaintext, just like their final local
    stores. No recovery passphrase is persisted. Journal paths are fixed, not
    interpreted from untrusted JSON.
    """
    names = ("credentials.json", "secrets.json", "bootstrap.yaml")

    def __init__(self, data):
        self.data = data
        self.directory = data / ".cloud-recovery"
        self.path = self.directory / "journal.json"

    def pending(self):
        if self.directory.is_symlink() or self.path.is_symlink():
            raise RecoveryIdentityConflict()
        if not self.path.exists():
            return None
        value = parse_json(self.path.read_bytes())
        if (not isinstance(value, dict) or set(value) != {"schema_version", "source_id", "node_id", "files"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or not isinstance(value["files"], dict) or set(value["files"]) != set(self.names)):
            raise RecoveryIdentityConflict()
        for name in self.names:
            path = self.directory / name
            if path.is_symlink() or checksum(path.read_bytes()) != value["files"][name]:
                raise RecoveryIdentityConflict()
        return value

    def publish(self, source_id, bootstrap, dataset, credential_bytes):
        pending = self.pending()
        content = {"credentials.json": credential_bytes, "secrets.json": canonical_bytes(dataset),
                   "bootstrap.yaml": yaml.safe_dump(bootstrap, sort_keys=False).encode("utf-8")}
        if pending:
            if (pending["source_id"] != source_id or pending["node_id"] != bootstrap["node_id"]
                    or any(pending["files"][name] != checksum(content[name]) for name in ("secrets.json", "bootstrap.yaml"))):
                raise RecoveryIdentityConflict()
        _private_directory(self.directory)
        # Remove a prior intent before refreshing stages; no bootstrap is written
        # in this window, and partial final files are checked on every rerun.
        if pending:
            self.path.unlink()
            LocalConfigStore._sync_directory(self.directory)
        for name, value in content.items():
            _atomic(self.directory / name, value)
        intent = {"schema_version": 1, "source_id": source_id, "node_id": bootstrap["node_id"],
                  "files": {name: checksum(value) for name, value in content.items()}}
        _atomic(self.path, canonical_bytes(intent))
        secret_name = hashlib.sha256(bootstrap["node_id"].encode("utf-8")).hexdigest() + ".json"
        targets = (self.data / "drive-credentials.json", self.data / "node-secrets" / secret_name,
                   self.data / "bootstrap.yaml")
        for name, target in zip(self.names, targets):
            _atomic(target, content[name])
        # All final files are durable before discarding the recovery intent.
        self.path.unlink()
        LocalConfigStore._sync_directory(self.directory)
        for name in self.names:
            (self.directory / name).unlink()
        self.directory.rmdir()
        LocalConfigStore._sync_directory(self.data)


@dataclass(frozen=True)
class RestoreResult:
    config_revision: int
    memory_revision: int | None
    provider: str


def restore_cloud_node(source, node_id, passphrase, credential_bytes, transport, *,
                       data_dir=None, default_path=None, activate=False):
    try:
        if type(activate) is not bool:
            raise RecoveryError()
        descriptor = validate_descriptor(source.descriptor)
        node = select_node(source, node_id)
        dataset = fetch_backup(transport, descriptor["nodes"][node]["secrets"], descriptor["source_id"], node, passphrase)
        credential_bytes = canonical_bytes(credential_payload(credential_bytes))
        data = Path(data_dir or Path(get_project_dir()) / "data")
        bootstrap = {"config_provider": "google_drive" if activate else "local", "node_id": node,
            "google_drive": {"folder_id": descriptor["folder_id"],
                             "manifest_file_id": descriptor["config_manifest_file_id"],
                             "credentials_path": str(data / "drive-credentials.json")}}
        if descriptor["memory_manifest_file_id"] is not None:
            bootstrap["google_drive"]["memory_manifest_file_id"] = descriptor["memory_manifest_file_id"]
        _check_existing(data, bootstrap, dataset)
        journal = _RestoreJournal(data)
        pending = journal.pending()
        if pending and (pending["source_id"] != descriptor["source_id"] or pending["node_id"] != node):
            raise RecoveryIdentityConflict()
        candidate = GoogleDriveConfigStore({**copy.deepcopy(bootstrap), "config_provider": "google_drive"},
            transport=transport, secret_provider=MemorySecrets(dataset), cache_dir=data / "cloud-config", default_path=default_path)
        config_revision, memory_revision = candidate.verify_recovery_readiness()
        # Pin the descriptor selection through preflight. A changed pointer or
        # authority requires a fresh selection, never silent last-writer-wins.
        content, etag = transport.read_manifest(source.file_id)
        if etag != source.etag or canonical_bytes(parse_json(content)) != canonical_bytes(descriptor):
            raise RecoveryConflict()
        # No directory or deployed identity is written until all checks pass.
        _private_directory(data)
        with portalocker.Lock(str(data / ".recovery.lock"), mode="a", timeout=5):
            _check_existing(data, bootstrap, dataset)
            try:
                journal.publish(descriptor["source_id"], bootstrap, dataset, credential_bytes)
            except RecoveryIdentityConflict:
                raise
            except Exception:
                raise RestorePublicationError() from None
        return RestoreResult(config_revision, memory_revision, bootstrap["config_provider"])
    except (RecoveryIdentityConflict, RestorePublicationError, RecoveryConflict):
        raise
    except ConfigConflict:
        raise RecoveryConflict() from None
    except Exception:
        raise RecoveryError() from None
