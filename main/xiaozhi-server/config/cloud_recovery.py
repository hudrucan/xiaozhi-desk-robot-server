"""Discoverable source metadata and authenticated, immutable node-secret backups."""

import base64
import copy
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from config.cloud_secrets import REFERENCE, SecretProvider
from config.config_store import ConfigConflict, canonical_bytes, checksum
from config.drive_transport import GoogleDriveTransport
from config.local_config import LocalConfigStore


class RecoveryError(Exception):
    message = "Cloud State recovery unavailable; verify authorization, selection and recovery passphrase."

    def __init__(self):
        super().__init__(self.message)


class RecoveryConflict(RecoveryError):
    message = "Cloud State recovery metadata changed; refresh and review before retrying."


class RecoveryIdentityConflict(RecoveryError):
    message = "Existing local identity or recovery transaction disagrees with the selected source/node; restore rejected."


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RecoveryError()
        result[key] = value
    return result


def parse_json(content):
    if not isinstance(content, bytes) or len(content) > 4 * 1024 * 1024:
        raise RecoveryError()
    return json.loads(content, object_pairs_hook=_object)


def source_uuid(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise RecoveryError()
    return value


def authority_source_id(folder_id, manifest_id):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "xiaozhi-cloud-state:"
                         + GoogleDriveTransport._id(folder_id) + ":" + GoogleDriveTransport._id(manifest_id)))


def safe_name(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 192
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise RecoveryError()
    return value


def validate_pointer(value):
    if (not isinstance(value, dict) or set(value) != {"file_id", "sha256"}
            or not isinstance(value.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"])):
        raise RecoveryError()
    GoogleDriveTransport._id(value["file_id"])


def validate_descriptor(value):
    if (not isinstance(value, dict) or set(value) != {"schema_version", "source_id", "label", "folder_id",
            "config_manifest_file_id", "memory_manifest_file_id", "nodes"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or not isinstance(value["nodes"], dict) or not value["nodes"]):
        raise RecoveryError()
    source_uuid(value["source_id"])
    safe_name(value["label"])
    for key in ("folder_id", "config_manifest_file_id"):
        GoogleDriveTransport._id(value[key])
    if value["memory_manifest_file_id"] is not None:
        GoogleDriveTransport._id(value["memory_manifest_file_id"])
    for node, entry in value["nodes"].items():
        safe_name(node)
        if not isinstance(entry, dict) or set(entry) != {"secrets"}:
            raise RecoveryError()
        validate_pointer(entry["secrets"])
    return value


def validate_dataset(value, node_id):
    if (not isinstance(value, dict) or set(value) != {"node_id", "values"}
            or value["node_id"] != node_id or not isinstance(value["values"], dict)
            or any(not isinstance(name, str) or not REFERENCE.fullmatch("${secret:" + name + "}")
                   or not isinstance(secret, str) for name, secret in value["values"].items())):
        raise RecoveryError()
    safe_name(node_id)
    return value


class MemorySecrets(SecretProvider):
    """Temporary restore provider: no filesystem reads/writes or secret logging."""
    def __init__(self, dataset):
        self.dataset = copy.deepcopy(validate_dataset(dataset, dataset["node_id"]))

    def get(self, name):
        if name not in self.dataset["values"]:
            raise RecoveryError()
        return self.dataset["values"][name]

    def put_many(self, values):
        raise RecoveryError()


def _aad(source_id, node_id):
    return canonical_bytes({"schema_version": 1, "source_id": source_uuid(source_id), "node_id": safe_name(node_id)})


def _key(passphrase, salt):
    if not isinstance(passphrase, str) or not passphrase or len(passphrase) > 4096:
        raise RecoveryError()
    return Scrypt(salt=salt, length=32, n=32768, r=8, p=1).derive(passphrase.encode("utf-8"))


def _encode(value):
    return base64.b64encode(value).decode("ascii")


def _decode(value):
    if not isinstance(value, str):
        raise RecoveryError()
    return base64.b64decode(value, validate=True)


def encrypt_dataset(dataset, source_id, node_id, passphrase):
    try:
        payload = canonical_bytes(validate_dataset(dataset, node_id))
        salt, nonce = os.urandom(16), os.urandom(12)
        encrypted = AESGCM(_key(passphrase, salt)).encrypt(nonce, payload, _aad(source_id, node_id))
        return canonical_bytes({"schema_version": 1, "source_id": source_id, "node_id": node_id,
            "kdf": {"name": "scrypt", "salt": _encode(salt), "n": 32768, "r": 8, "p": 1},
            "cipher": {"name": "AES-256-GCM", "nonce": _encode(nonce), "ciphertext": _encode(encrypted)}})
    except Exception:
        raise RecoveryError() from None


def decrypt_dataset(content, source_id, node_id, passphrase):
    try:
        value = parse_json(content)
        if (not isinstance(value, dict) or set(value) != {"schema_version", "source_id", "node_id", "kdf", "cipher"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["source_id"] != source_id or value["node_id"] != node_id):
            raise RecoveryError()
        kdf, cipher = value["kdf"], value["cipher"]
        if (not isinstance(kdf, dict) or set(kdf) != {"name", "salt", "n", "r", "p"}
                or kdf["name"] != "scrypt"
                or any(type(kdf[key]) is not int or kdf[key] != expected for key, expected in (("n", 32768), ("r", 8), ("p", 1)))
                or not isinstance(cipher, dict) or set(cipher) != {"name", "nonce", "ciphertext"}
                or cipher["name"] != "AES-256-GCM"):
            raise RecoveryError()
        salt, nonce, encrypted = _decode(kdf["salt"]), _decode(cipher["nonce"]), _decode(cipher["ciphertext"])
        if len(salt) != 16 or len(nonce) != 12 or len(encrypted) < 16:
            raise RecoveryError()
        plaintext = AESGCM(_key(passphrase, salt)).decrypt(nonce, encrypted, _aad(source_id, node_id))
        dataset = validate_dataset(parse_json(plaintext), node_id)
        if canonical_bytes(dataset) != plaintext:
            raise RecoveryError()
        return dataset
    except Exception:
        raise RecoveryError() from None


@dataclass(frozen=True)
class DiscoveredSource:
    file_id: str
    etag: str
    descriptor: dict

    def metadata(self):
        return {key: copy.deepcopy(self.descriptor[key]) for key in ("source_id", "label")} | {
            "nodes": sorted(self.descriptor["nodes"])}


def discover_sources(transport):
    try:
        sources, identities, authorities = [], set(), set()
        for marker in transport.list_state_descriptors():
            content, etag = transport.read_manifest(marker["file_id"])
            descriptor = validate_descriptor(parse_json(content))
            authority = (descriptor["folder_id"], descriptor["config_manifest_file_id"])
            if (descriptor["source_id"] != marker["source_id"] or not isinstance(etag, str) or not etag
                    or descriptor["source_id"] in identities or authority in authorities):
                raise RecoveryConflict()
            identities.add(descriptor["source_id"])
            authorities.add(authority)
            sources.append(DiscoveredSource(marker["file_id"], etag, descriptor))
        return sources
    except (ConfigConflict, RecoveryConflict):
        raise RecoveryConflict() from None
    except Exception:
        raise RecoveryError() from None


def fetch_backup(transport, pointer, source_id, node_id, passphrase):
    try:
        validate_pointer(pointer)
        content = transport.download(pointer["file_id"])
        if checksum(content) != pointer["sha256"]:
            raise RecoveryError()
        return decrypt_dataset(content, source_id, node_id, passphrase)
    except Exception:
        raise RecoveryError() from None


def _retained_memory_id(bootstrap, descriptor, transport, secrets, default_path, cache_dir):
    """A non-Memory node must not erase another node's source-wide authority."""
    identifier = bootstrap["google_drive"].get("memory_manifest_file_id")
    if identifier is not None or descriptor["memory_manifest_file_id"] is None:
        return identifier
    from config.cloud_layers import centralized, resolve_layers
    from config.cloud_memory import CloudMemoryStore
    from config.config_loader import merge_configs
    from config.google_drive_config import GoogleDriveConfigStore
    from config.memory_reconciliation import explicit_memory_config
    candidate = GoogleDriveConfigStore({**copy.deepcopy(bootstrap), "config_provider": "google_drive"},
        transport=transport, secret_provider=secrets, default_path=default_path, cache_dir=cache_dir)
    candidate._load_cache()
    snapshot, _ = candidate._cloud_snapshot()
    obj, defaults = snapshot["payload"]["object"], candidate._repo_defaults()
    nodes = obj["layers"]["nodes"] if centralized(obj) else [bootstrap["node_id"]]
    retained = descriptor["memory_manifest_file_id"]
    required = False
    for node in nodes:
        base, overrides = resolve_layers(obj, node, defaults)
        provider = explicit_memory_config(merge_configs(base, overrides))
        if provider is not None:
            metadata = copy.deepcopy(bootstrap)
            metadata["node_id"] = node
            metadata["google_drive"]["memory_manifest_file_id"] = retained
            CloudMemoryStore(metadata, transport, candidate.cache_dir.parent / "cloud-memory", provider).preflight()
            required = True
    return retained if required else None


def publish_recovery_descriptor(bootstrap, transport, secrets, passphrase, *, label=None, rotate=False,
                                creation_receipt=None, default_path=None, cache_dir=None):
    """Verified authorities are inputs; only descriptor CAS changes metadata."""
    try:
        if type(rotate) is not bool:
            raise RecoveryError()
        drive, node = bootstrap["google_drive"], bootstrap["node_id"]
        safe_name(node)
        dataset = validate_dataset(secrets.export_dataset(), node)
        sources = discover_sources(transport)
        matches = [item for item in sources if (item.descriptor["folder_id"], item.descriptor["config_manifest_file_id"])
                   == (drive["folder_id"], drive["manifest_file_id"])]
        current = matches[0] if matches else None
        # Deterministic source identity survives a lost initial upload response.
        source_id = current.descriptor["source_id"] if current else authority_source_id(drive["folder_id"], drive["manifest_file_id"])
        descriptor = copy.deepcopy(current.descriptor) if current else {
            "schema_version": 1, "source_id": source_id, "label": label or "Desk Robot",
            "folder_id": drive["folder_id"], "config_manifest_file_id": drive["manifest_file_id"],
            "memory_manifest_file_id": None, "nodes": {}}
        if label is not None:
            descriptor["label"] = safe_name(label)
        descriptor["memory_manifest_file_id"] = _retained_memory_id(bootstrap, descriptor, transport, secrets, default_path, cache_dir)
        previous = descriptor["nodes"].get(node)
        pointer = None
        if previous and not rotate:
            old = fetch_backup(transport, previous["secrets"], source_id, node, passphrase)
            if canonical_bytes(old) == canonical_bytes(dataset):
                pointer = previous["secrets"]
        if pointer is None:
            content = encrypt_dataset(dataset, source_id, node, passphrase)
            file_id = transport.upload_immutable(drive["folder_id"], content, "node-secrets.enc.json")
            pointer = {"file_id": file_id, "sha256": checksum(content)}
            restored = fetch_backup(transport, pointer, source_id, node, passphrase)
            if canonical_bytes(restored) != canonical_bytes(dataset):
                raise RecoveryError()
        descriptor["nodes"][node] = {"secrets": pointer}
        content = canonical_bytes(validate_descriptor(descriptor))
        if current:
            if canonical_bytes(current.descriptor) != content:
                transport.replace_manifest(current.file_id, content, current.etag)
            file_id = current.file_id
        else:
            # Recheck discovery after encryption/upload to detect concurrent creators.
            if any(item.descriptor["source_id"] == source_id for item in discover_sources(transport)):
                raise RecoveryConflict()
            if creation_receipt is None:
                from config.config_loader import get_project_dir
                creation_receipt = Path(get_project_dir()) / f"data/cloud-provisioning/{source_id}.descriptor.json"
            receipt = Path(creation_receipt)
            if receipt.exists() or receipt.is_symlink() or receipt.parent.is_symlink():
                # Discovery has not recovered an interrupted creation. Never
                # guess that a lost response means no descriptor was created.
                raise RecoveryConflict()
            receipt.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            LocalConfigStore(receipt.parent / ".unused.yaml")._atomic_bytes(receipt, canonical_bytes({
                "schema_version": 1, "source_id": source_id, "folder_id": drive["folder_id"],
                "config_manifest_file_id": drive["manifest_file_id"]}))
            file_id = transport.create_state_descriptor(drive["folder_id"], content, source_id)
        observed = [item for item in discover_sources(transport) if item.file_id == file_id]
        if len(observed) != 1 or canonical_bytes(observed[0].descriptor) != content:
            raise RecoveryConflict()
        return file_id
    except (ConfigConflict, RecoveryConflict):
        raise RecoveryConflict() from None
    except Exception:
        raise RecoveryError() from None


def backup_cloud_node(bootstrap, passphrase, *, transport=None, secrets=None, label=None, rotate=False,
                      cache_dir=None, default_path=None, creation_receipt=None):
    """Explicit backup of an already-provisioned node; no Config/Memory mutation."""
    try:
        from config.cloud_secrets import LocalSecretStore
        from config.google_drive_config import GoogleDriveConfigStore
        secrets = secrets or LocalSecretStore(bootstrap["node_id"])
        transport = transport or GoogleDriveTransport(bootstrap["google_drive"]["credentials_path"])
        candidate = GoogleDriveConfigStore({**copy.deepcopy(bootstrap), "config_provider": "google_drive"},
            transport=transport, secret_provider=secrets, cache_dir=cache_dir, default_path=default_path)
        config_revision, memory_revision = candidate.verify_recovery_readiness()
        metadata = copy.deepcopy(bootstrap)
        if memory_revision is None:
            metadata["google_drive"].pop("memory_manifest_file_id", None)
        descriptor_id = publish_recovery_descriptor(metadata, transport, secrets, passphrase,
            label=label, rotate=rotate, creation_receipt=creation_receipt, default_path=default_path, cache_dir=cache_dir)
        return config_revision, memory_revision, descriptor_id
    except RecoveryConflict:
        raise
    except Exception:
        raise RecoveryError() from None
