"""Reference-only cloud configuration and replaceable node-local secrets."""

import copy
import hashlib
import json
import os
import re
import threading
import uuid
from abc import ABC, abstractmethod
from contextlib import contextmanager
from pathlib import Path

import portalocker

from config.config_loader import get_project_dir
from config.local_config import LocalConfigStore
from core.utils.config_secrets import is_secret_name

REFERENCE = re.compile(r"\$\{secret:([A-Za-z0-9_][A-Za-z0-9_.-]{0,191})\}")
_LOCK = threading.RLock()


def placeholder(value):
    if value is None or value == "":
        return True
    return isinstance(value, str) and (
        not value.strip() or value.lower().startswith(("your_", "your-"))
        or "你的" in value
    )


def _walk(value, secret_transform, secret=False):
    if secret:
        return secret_transform(value)
    if isinstance(value, dict):
        return {key: _walk(child, secret_transform, is_secret_name(key))
                for key, child in value.items()}
    if isinstance(value, list):
        return [_walk(child, secret_transform) for child in value]
    # References in public fields could expose resolved values via Settings.
    if isinstance(value, str) and "${secret:" in value:
        raise ValueError("Secret references must occupy a complete secret field")
    return copy.deepcopy(value)


def validate_cloud_secrets(value):
    def validate(secret):
        if not placeholder(secret) and not (
            isinstance(secret, str) and REFERENCE.fullmatch(secret)
        ):
            raise ValueError("Cloud secret fields require references or empty placeholders")
        return secret
    _walk(value, validate)


class SecretProvider(ABC):
    """Settings supplies values; only references cross the cloud boundary."""

    @abstractmethod
    def get(self, name): ...

    @abstractmethod
    def put_many(self, values): ...

    def externalize(self, config):
        pending = {}

        def convert(value):
            if placeholder(value) or (isinstance(value, str) and REFERENCE.fullmatch(value)):
                return copy.deepcopy(value)
            if not isinstance(value, str) or "${secret:" in value:
                raise ValueError("Configured secret must be a string or a complete reference")
            # Never overwrite an active revision's reference. A failed CAS may
            # leave unused local entries, but cannot alter active secret values.
            name = "VALUE_" + uuid.uuid4().hex
            pending[name] = value
            return "${secret:" + name + "}"

        return _walk(config, convert), pending

    def resolve(self, config):
        def resolve(value):
            match = REFERENCE.fullmatch(value) if isinstance(value, str) else None
            return self.get(match[1]) if match else copy.deepcopy(value)
        return _walk(config, resolve)


class LocalSecretStore(SecretProvider):
    """Private atomic JSON per local node, independent of cloud/LKG caches."""

    def __init__(self, node_id, directory=None):
        self.node_id = node_id
        self.directory = Path(directory or Path(get_project_dir()) / "data/node-secrets")
        identity = hashlib.sha256(node_id.encode("utf-8")).hexdigest()
        self.path = self.directory / (identity + ".json")
        self.io = LocalConfigStore(self.directory / ".unused.yaml")

    @contextmanager
    def locked(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink() or self.path.is_symlink():
            raise ValueError("Local secret storage must not use symlinks")
        os.chmod(self.directory, 0o700)
        with _LOCK, portalocker.Lock(str(self.directory / ".secrets.lock"), mode="a", timeout=5):
            yield

    def _read(self):
        if not self.path.exists():
            return {}
        os.chmod(self.path, 0o600)
        value = json.loads(self.path.read_bytes())
        if (not isinstance(value, dict) or value.get("node_id") != self.node_id
                or not isinstance(value.get("values"), dict)
                or any(not isinstance(k, str) or not isinstance(v, str)
                       for k, v in value["values"].items())):
            raise ValueError("Invalid local secret storage")
        return value["values"]

    def get(self, name):
        with self.locked():
            values = self._read()
            if name not in values:
                # Neither values nor reference names are included in errors.
                raise ValueError("Required node-local secret is unavailable")
            return values[name]

    def put_many(self, values):
        if not values:
            return
        if any(not isinstance(k, str) or not REFERENCE.fullmatch("${secret:" + k + "}")
               or not isinstance(v, str) for k, v in values.items()):
            raise ValueError("Invalid local secret entry")
        with self.locked():
            current = self._read()
            # Named references are immutable too; rotate with a new name.
            if any(k in current and current[k] != v for k, v in values.items()):
                raise ValueError("Existing secret reference is immutable; use a new name")
            current.update(values)
            content = json.dumps({"node_id": self.node_id, "values": current},
                                 sort_keys=True, ensure_ascii=False).encode("utf-8")
            self.io._atomic_bytes(self.path, content)
