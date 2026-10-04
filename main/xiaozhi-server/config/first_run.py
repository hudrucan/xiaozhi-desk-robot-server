"""Durable first-run ownership, without loading configuration or providers."""

import os
from pathlib import Path

from config.config_loader import get_project_dir


class FirstRunError(Exception):
    def __init__(self):
        super().__init__("First-run state unavailable; inspect local setup files before retrying.")


def _exists(path):
    return os.path.lexists(path)


def fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _create(path, content):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    fsync_directory(path.parent)


def prepare_first_run(data_dir=None):
    data = Path(data_dir or Path(get_project_dir()) / "data")
    marker = data / ".first-run"
    try:
        if data.is_symlink():
            raise FirstRunError()
        if _exists(marker):
            if marker.is_symlink() or not marker.is_file() or marker.read_bytes() != b"first-run-v1\n":
                raise FirstRunError()
            return True
        if any(_exists(data / name) for name in ("bootstrap.yaml", ".config.yaml", "config.d")):
            return False
        # OAuth may already be prepared by the CLI. Other local state is not an
        # empty installation and must follow normal validation, never be reset.
        if data.exists() and any(item.name not in {"oauth-client.json", "drive-credentials.json"} for item in data.iterdir()):
            return False
        data.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(data, 0o700)
        fsync_directory(data.parent)
        # Publish ownership first. A crash before the seed write must still
        # resume setup, not mistake our seed for a legacy Local installation.
        _create(marker, b"first-run-v1\n")
        _create(data / ".config.yaml", b"{}\n")
        return True
    except FirstRunError:
        raise
    except Exception:
        raise FirstRunError() from None


def finish_first_run(data_dir):
    """Called only after the existing restore transaction fully returned."""
    data = Path(data_dir)
    marker = data / ".first-run"
    try:
        if data.is_symlink() or marker.is_symlink() or marker.read_bytes() != b"first-run-v1\n":
            raise FirstRunError()
        marker.unlink()
        try:
            fsync_directory(data)
        except Exception:
            # Keep setup pending when durable marker removal cannot be proven.
            _create(marker, b"first-run-v1\n")
            raise
    except Exception:
        raise FirstRunError() from None
