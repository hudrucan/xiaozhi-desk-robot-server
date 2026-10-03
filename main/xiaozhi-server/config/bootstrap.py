"""Local-only node identity and configuration source selection."""

from pathlib import Path
import threading

import portalocker

import yaml

from config.config_loader import get_project_dir
from config.local_config import LocalConfigStore

PROVIDERS = {"local", "google_drive"}
_BOOTSTRAP_LOCK = threading.RLock()


def load_bootstrap(path=None):
    path = Path(path or Path(get_project_dir()) / "data/bootstrap.yaml")
    value = LocalConfigStore._read_object(path) if path.exists() else {}
    bootstrap = {"config_provider": "local", "node_id": "mac-dev", **value}
    if not isinstance(bootstrap["config_provider"], str) or bootstrap["config_provider"] not in PROVIDERS:
        raise ValueError("Unsupported config_provider in local bootstrap")
    if not isinstance(bootstrap["node_id"], str) or not bootstrap["node_id"].strip():
        raise ValueError("Bootstrap node_id must be a non-empty string")
    if bootstrap["config_provider"] == "google_drive":
        drive = bootstrap.get("google_drive")
        if not isinstance(drive, dict) or any(
            not isinstance(drive.get(key), str) or not drive[key].strip()
            for key in ("folder_id", "manifest_file_id", "credentials_path")
        ):
            raise ValueError("Google Drive requires folder_id, manifest_file_id and credentials_path")
    return bootstrap


def save_bootstrap(bootstrap, path=None):
    path = Path(path or Path(get_project_dir()) / "data/bootstrap.yaml")
    store = LocalConfigStore(path.parent / ".config.yaml")
    store.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Source selection may hold the Local config lock. Use a separate lock so
    # bootstrap publication cannot recursively acquire that file lock.
    with _BOOTSTRAP_LOCK, portalocker.Lock(
        str(store.directory / ".bootstrap.lock"), mode="a", timeout=5
    ):
        store._atomic_bytes(path, yaml.safe_dump(bootstrap, sort_keys=False).encode())
