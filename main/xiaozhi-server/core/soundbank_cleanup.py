"""Reference-aware cleanup of retired and orphaned soundbank audio."""

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
from collections.abc import Mapping

from config.config_loader import get_project_dir
from core.soundbank import (
    SOUNDBANK_EXTENSIONS,
    SoundbankError,
    resolve_soundbank_asset,
    resolve_soundbank_root,
    soundbank_entry_filename,
    soundbank_entry_optimized,
)


class SoundbankCleanup:
    def __init__(self, runtime_config):
        self.runtime_config = runtime_config
        # The handler holds this lock across generation, publication and draft
        # registration; the editor holds it across config saves and cleanup.
        self.lock = threading.RLock()
        self.drafts = {}
        self.owned_drafts = {}
        self.journal = Path(get_project_dir()) / "data/.soundbank-cleanup.json"

    @staticmethod
    def _asset_reference(root, filename, managed_only):
        path = resolve_soundbank_asset(root, filename)
        if managed_only:
            # Resolution is useful for shared-reference protection, but must
            # never turn a retired alias into deletion of its target.
            relative = Path(filename.strip().replace("\\", "/"))
            cursor = root
            for part in relative.parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    return None
        return path

    @staticmethod
    def _references(config, managed_only=False):
        soundbank = config.get("static_soundbank", {})
        root = resolve_soundbank_root(soundbank)
        entries = soundbank.get("entries", {})
        if not isinstance(entries, Mapping):
            raise SoundbankError("static_soundbank.entries must be an object")
        paths = set()
        for entry in entries.values():
            canonical = soundbank_entry_filename(entry)
            provenance = entry.get("generated_by") if isinstance(entry, Mapping) else None
            generated = isinstance(provenance, Mapping) and isinstance(
                provenance.get("provider"), str
            ) and bool(provenance["provider"].strip())
            if not managed_only or generated:
                paths.add(SoundbankCleanup._asset_reference(root, canonical, managed_only))
            optimized = soundbank_entry_optimized(entry)
            if optimized is not None:
                optimized_path = SoundbankCleanup._asset_reference(
                    root, optimized.get("file"), managed_only
                )
                if managed_only and optimized_path is not None and (
                    optimized_path.suffix.lower() != ".p3"
                    or (not generated and optimized_path == resolve_soundbank_asset(root, canonical))
                ):
                    continue
                paths.add(optimized_path)
        paths.discard(None)
        return paths

    def protect_draft(self, entry, owner=None, owned_canonical=True):
        config = {"static_soundbank": {
            "directory": resolve_soundbank_root(
                self.runtime_config.get("static_soundbank", {})
            ).as_posix(),
            "entries": {"draft": entry},
        }}
        paths = self._references(config)
        self.drafts.setdefault(owner, set()).update(paths)
        if not owned_canonical:
            paths.discard(resolve_soundbank_asset(
                resolve_soundbank_root(config["static_soundbank"]), entry["file"]
            ))
        self.owned_drafts.setdefault(owner, set()).update(paths)

    @staticmethod
    def _record(root, path):
        """Reject symlinks (including parent directories), even inside root."""
        relative = path.relative_to(root)
        if root.is_symlink():
            return None
        cursor = root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                return None
        try:
            info = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode):
            return None
        return {
            "root": str(root), "file": relative.as_posix(),
            "size": info.st_size, "mtime_ns": info.st_mtime_ns,
            "device": info.st_dev, "inode": info.st_ino,
        }

    @staticmethod
    def _unlink_record(record):
        """Bind directory descriptors so replaced symlinks cannot redirect unlink."""
        root = Path(record["root"])
        relative = Path(record["file"])
        # Resolve/validate before opening, then walk the original lexical path
        # without following symlinks in any directory component.
        resolve_soundbank_asset(root, record["file"])
        descriptor = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in (*root.parts[1:], *relative.parts[:-1]):
                next_descriptor = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = next_descriptor
            info = os.stat(relative.name, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or (
                info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino
            ) != (
                record["size"], record["mtime_ns"], record["device"], record["inode"]
            ):
                raise SoundbankError(f"Asset changed: {record['file']}")
            os.unlink(relative.name, dir_fd=descriptor)
        finally:
            os.close(descriptor)

    def _read_pending(self):
        if not self.journal.exists():
            return []
        try:
            records = json.loads(self.journal.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise SoundbankError(f"Cannot read soundbank cleanup journal: {error}") from error
        if not isinstance(records, list) or any(
            not isinstance(item, dict)
            or set(item) != {"root", "file", "size", "mtime_ns", "device", "inode"}
            or not isinstance(item["root"], str)
            or not Path(item["root"]).is_absolute()
            or not isinstance(item["file"], str)
            or any(type(item[key]) is not int for key in ("size", "mtime_ns", "device", "inode"))
            for item in records
        ):
            raise SoundbankError("Invalid soundbank cleanup journal")
        return records

    def _write_pending(self, records):
        if not records and not self.journal.exists():
            return
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix=".soundbank-cleanup-", dir=self.journal.parent
        )
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(records, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.journal)
            directory = os.open(self.journal.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def prepare_save(self, previous, updated, owner=None, retired_drafts=()):
        """Persist intent before config publication; saved references guard retries."""
        pending = self._read_pending()
        known = {(item["root"], item["file"]) for item in pending}
        updated_paths = self._references(updated)
        retired = self._references(previous, managed_only=True) - updated_paths
        root = resolve_soundbank_root(previous.get("static_soundbank", {}))
        runtime_root = resolve_soundbank_root(self.runtime_config.get("static_soundbank", {}))
        draft_paths = {resolve_soundbank_asset(runtime_root, filename) for filename in retired_drafts}
        draft_paths &= self.owned_drafts.get(owner, set())
        draft_paths -= updated_paths
        for path in sorted(retired):
            record = self._record(root, path)
            if record and (record["root"], record["file"]) not in known:
                pending.append(record)
                known.add((record["root"], record["file"]))
        for path in sorted(draft_paths):
            record = self._record(runtime_root, path)
            if record and (record["root"], record["file"]) not in known:
                pending.append(record)
        self._write_pending(pending)

    def _protected(self, saved_config):
        drafts = set().union(*self.drafts.values())
        return self._references(saved_config) | self._references(self.runtime_config) | drafts

    def after_save(self, saved_config, owner=None, retired_drafts=()):
        released = self._references(saved_config)
        root = resolve_soundbank_root(self.runtime_config.get("static_soundbank", {}))
        released.update(resolve_soundbank_asset(root, filename) for filename in retired_drafts)
        self.drafts.get(owner, set()).difference_update(released)
        self.owned_drafts.get(owner, set()).difference_update(released)
        return self.cleanup_pending(saved_config)

    def cleanup_pending(self, saved_config):
        protected = self._protected(saved_config)
        remaining = []
        deleted = 0
        freed = 0
        errors = []
        for record in self._read_pending():
            try:
                root = Path(record["root"])
                path = resolve_soundbank_asset(root, record["file"])
                if path in protected:
                    remaining.append(record)
                    continue
                # A replaced file at the same name is a different asset. Never
                # apply an old cleanup intent to it.
                if self._record(root, path) != record:
                    continue
                self._unlink_record(record)
                deleted += 1
                freed += record["size"]
            except (OSError, ValueError, SoundbankError) as error:
                remaining.append(record)
                errors.append(str(error))
        self._write_pending(remaining)
        return {"deleted": deleted, "bytes": freed, "pending": len(remaining), "errors": errors}

    def unused(self, saved_config, confirmation=None):
        """Preview, then revalidate the same file identities before manual deletion."""
        root = resolve_soundbank_root(saved_config.get("static_soundbank", {}))
        protected = self._protected(saved_config)
        records = []
        # os.walk does not follow directory symlinks; hidden/temp directories
        # and non-audio files are outside the cleanup scope.
        for directory, names, files in os.walk(root, followlinks=False):
            names[:] = sorted(name for name in names if not name.startswith(".")
                              and not (Path(directory) / name).is_symlink())
            for name in sorted(files):
                path = Path(directory) / name
                if name.startswith(".") or path.suffix.lower() not in SOUNDBANK_EXTENSIONS:
                    continue
                if path in protected:
                    continue
                record = self._record(root, path)
                if record:
                    records.append(record)
        token = hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()
        result = {
            "count": len(records), "bytes": sum(item["size"] for item in records),
            "files": [item["file"] for item in records], "token": token,
        }
        if confirmation is not None:
            if confirmation != token:
                raise SoundbankError("Unused sounds changed. Preview cleanup again.", status=409)
            deleted = 0
            freed = 0
            errors = []
            for record in records:
                try:
                    path = resolve_soundbank_asset(root, record["file"])
                    if self._record(root, path) != record:
                        raise SoundbankError(f"Asset changed: {record['file']}")
                    self._unlink_record(record)
                    deleted += 1
                    freed += record["size"]
                except (OSError, ValueError, SoundbankError) as error:
                    errors.append(str(error))
            result.update(deleted=deleted, bytes=freed, errors=errors)
        return result
