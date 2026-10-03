"""Desired configuration persistence and process-owned active configuration."""

import copy
import hashlib
import json
import threading
from abc import ABC, abstractmethod
from pathlib import Path

import yaml

from config.bootstrap import load_bootstrap, save_bootstrap
from config.config_loader import get_project_dir, load_default_config, merge_configs
from config.local_config import LocalConfigStore


class ConfigConflict(ValueError):
    pass


class ConfigUnavailable(OSError):
    pass


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def checksum(content):
    return hashlib.sha256(content).hexdigest()


class ConfigStore(ABC):
    """All *_unlocked operations require locked(); commit validates layers."""

    def __init__(self, bootstrap, default_path=None, validator=None):
        self.bootstrap = copy.deepcopy(bootstrap)
        self.default_path = default_path or str(Path(get_project_dir()) / "config.yaml")
        if validator is None:
            from config.config_validation import validate_config
            validator = validate_config
        self.validator = validator
        self.active_revision = None
        self.runtime_snapshot = None
        self.pending_provider = None
        self.conflict = False

    @abstractmethod
    def locked(self): ...

    @abstractmethod
    def read_unlocked(self): ...

    @abstractmethod
    def defaults_unlocked(self): ...

    @abstractmethod
    def revision_unlocked(self): ...

    @abstractmethod
    def commit_unlocked(self, config, base_revision=None): ...

    def prepare_commit_unlocked(self, base_revision=None):
        if self.pending_provider:
            raise ValueError("Restart to finish switching configuration source before saving")

    def refresh_unlocked(self, strict=False):
        return self.read_unlocked()

    def validate_runtime_readiness(self):
        """Validate a candidate without selecting runtime or marking it active."""
        with self.locked():
            self.refresh_unlocked(strict=True)
            self.validator(merge_configs(self.defaults_unlocked(), self.read_unlocked()))

    def prepare_runtime(self):
        with self.locked():
            self.refresh_unlocked()
            layers = {"defaults": self.defaults_unlocked(), "overrides": self.read_unlocked()}
            self.runtime_snapshot = (self.revision_unlocked(), copy.deepcopy(layers))
            return merge_configs(layers["defaults"], layers["overrides"])

    def mark_applied(self):
        """Called only after the configuration selected at boot starts successfully."""
        with self.locked():
            if self.runtime_snapshot is None:
                raise ValueError("No boot configuration has been selected")
            self.active_revision = self.runtime_snapshot[0]

    def status_unlocked(self):
        revision = self.revision_unlocked()
        return {
            "config_provider": self.bootstrap["config_provider"],
            "node_id": self.bootstrap["node_id"],
            "desired_revision": revision,
            "active_revision": self.active_revision,
            "sync_state": "in_sync" if revision == self.active_revision else "out_of_sync",
            "conflict": self.conflict,
            "pending_provider": self.pending_provider,
            "restart_required": revision != self.active_revision or bool(self.pending_provider),
            "last_sync": None,
            "last_error": None,
        }


class LocalConfigStoreAdapter(ConfigStore):
    """Delegate the existing sectioned transaction without changing its format."""

    def __init__(self, bootstrap, local_path=None, **kwargs):
        super().__init__(bootstrap, **kwargs)
        self.local = LocalConfigStore(local_path)

    def locked(self):
        return self.local.locked()

    def read_unlocked(self):
        return self.local.read_unlocked()

    def defaults_unlocked(self):
        return load_default_config(self.default_path)

    def revision_unlocked(self):
        # Local YAML may retain unknown roots with dates or other safe YAML
        # values. Revision metadata must not impose the cloud JSON format.
        return checksum(yaml.safe_dump({
            "defaults": self.defaults_unlocked(), "overrides": self.read_unlocked(),
        }, allow_unicode=True, sort_keys=True).encode("utf-8"))

    def commit_unlocked(self, config, base_revision=None):
        self.validator(merge_configs(self.defaults_unlocked(), config))
        self.local.write_unlocked(config)

    def status_unlocked(self):
        result = super().status_unlocked()
        path = "data/config.d/" if self.local.sections_dir.exists() else "data/.config.yaml"
        result.update(source=path, cache_path=None, active_source=path, sync_status="local")
        return result


_STORE = None
_STORE_LOCK = threading.RLock()


def create_config_store(bootstrap=None, **kwargs):
    bootstrap = bootstrap or load_bootstrap()
    if bootstrap["config_provider"] == "local":
        return LocalConfigStoreAdapter(bootstrap, **kwargs)
    from config.google_drive_config import GoogleDriveConfigStore
    return GoogleDriveConfigStore(bootstrap, **kwargs)


def get_config_store():
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = create_config_store()
        return _STORE


def stage_provider_switch(provider):
    """Select a pre-provisioned source; never copy or merge across providers."""
    from config.bootstrap import PROVIDERS
    if provider not in PROVIDERS:
        raise ValueError("Unsupported configuration provider")
    store = get_config_store()
    with store.locked():
        if provider == store.bootstrap["config_provider"]:
            raise ValueError("Configuration provider is already selected")
        candidate_bootstrap = {**load_bootstrap(), "config_provider": provider}
        # Readiness includes node-local secret resolution for cloud candidates.
        candidate = create_config_store(candidate_bootstrap)
        if provider == "local" and not candidate.local.local_path.is_file():
            raise ValueError("Create data/.config.yaml before switching to Local")
        candidate.validate_runtime_readiness()
        save_bootstrap(candidate_bootstrap)
        store.pending_provider = provider
        return store.status_unlocked()
