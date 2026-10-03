"""Publish immutable sound assets and materialize verified files before cloud boot."""

import copy
import hashlib
import os
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path

from config.cloud_layers import centralized, legacy_centralized, resolve_layers
from config.config_loader import merge_configs
from config.config_store import ConfigUnavailable
from core.soundbank import (
    SOUNDBANK_MIME_TYPES, SoundbankError, resolve_soundbank_asset,
    resolve_soundbank_root, soundbank_entry_filename, soundbank_entry_optimized,
    soundbank_p3_sample_rate, validate_soundbank_cloud_metadata,
    validate_soundbank_cloud_pointer,
)
from core.utils import p3


def _digest(content):
    return hashlib.sha256(content).hexdigest()


def _matches(content, pointer):
    return len(content) == pointer["size"] and _digest(content) == pointer["sha256"]


def _assets(config, *, pointer_only=False):
    soundbank = config.get("static_soundbank", {})
    entries = soundbank.get("entries", {})
    if not entries:
        return
    root = resolve_soundbank_root(soundbank)
    for entry in entries.values():
        metadata = entry if isinstance(entry, Mapping) else {"file": soundbank_entry_filename(entry)}
        for asset, optimized in ((metadata, False), (soundbank_entry_optimized(entry), True)):
            if asset is None or (pointer_only and "cloud" not in asset):
                continue
            filename = soundbank_entry_filename(asset)
            path = resolve_soundbank_asset(root, filename)
            if optimized and path.suffix.lower() != ".p3":
                raise ValueError("Optimized soundbank asset must use P3")
            yield root, filename, path, asset, optimized


def _validate_p3(content, path, config, metadata, optimized):
    if path.suffix.lower() == ".p3":
        p3.load_validated_opus_bytes(
            content, sample_rate=soundbank_p3_sample_rate(config, metadata if optimized else None),
            frame_duration_ms=p3.P3_FRAME_DURATION_MS,
        )


def _safe_target(root, filename, *, create=False):
    """Use the runtime resolver, and reject symlinks for materialization writes."""
    path = resolve_soundbank_asset(root, filename)
    if root.is_symlink():
        raise ValueError("Cloud soundbank destination cannot use symlinks")
    lexical = root / Path(*filename.strip().replace("\\", "/").split("/"))
    # A write must never replace a symlink's target, even inside the soundbank.
    cursor = root
    for part in lexical.relative_to(root).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("Cloud soundbank materialization cannot use symlinks")
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
        if resolve_soundbank_asset(root, filename) != path:
            raise ValueError("Cloud soundbank destination changed")
    return path


def _replace_staged(temporary, root, filename, target):
    """Bind directory descriptors, as cleanup does, so symlinks cannot redirect writes."""
    if _safe_target(root, filename) != target:
        raise ValueError("Cloud soundbank destination changed")
    relative = target.relative_to(root)
    descriptor = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in (*root.parts[1:], *relative.parts[:-1]):
            next_descriptor = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        try:
            info = os.stat(relative.name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        if info is not None and not stat.S_ISREG(info.st_mode):
            raise ValueError("Cloud soundbank destination is not a regular file")
        os.replace(temporary.name, relative.name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class CloudSoundbankAssets:
    def __init__(self, transport, folder_id, cache_dir):
        self.transport = transport
        self.folder_id = folder_id
        self.cache_dir = Path(cache_dir).resolve()

    def _check_runtime_root(self, root):
        if self.cache_dir.is_relative_to(root) or root.is_relative_to(self.cache_dir):
            raise ValueError("Cloud asset cache must be outside runtime soundbank")

    def _cache_target(self, root, path, pointer):
        self._check_runtime_root(root)
        return _safe_target(self.cache_dir, pointer["sha256"] + path.suffix.lower())

    @staticmethod
    def _read_verified(path, pointer, config, metadata, optimized):
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            return None
        if not _matches(content, pointer):
            return None
        _validate_p3(content, path, config, metadata, optimized)
        return content

    @staticmethod
    def _stage(content, target):
        descriptor, name = tempfile.mkstemp(prefix=".cloud-soundbank-", suffix=target.suffix, dir=target.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return temporary

    def _cache_content(self, root, path, metadata, content, config, optimized):
        pointer = metadata["cloud"]
        target = self._cache_target(root, path, pointer)
        if self._read_verified(target, pointer, config, metadata, optimized) is not None:
            return
        target = _safe_target(self.cache_dir, target.name, create=True)
        temporary = self._stage(content, target)
        try:
            if self._read_verified(temporary, pointer, config, metadata, optimized) is None:
                raise ValueError("Cloud soundbank cache verification failed")
            _replace_staged(temporary, self.cache_dir, target.name, target)
        finally:
            temporary.unlink(missing_ok=True)

    def _obtain(self, root, filename, path, metadata, optimized, config):
        pointer = metadata["cloud"]
        target = _safe_target(root, filename)
        content = self._read_verified(target, pointer, config, metadata, optimized)
        if content is not None:
            self._cache_content(root, path, metadata, content, config, optimized)
            return content
        cached = self._cache_target(root, path, pointer)
        content = self._read_verified(cached, pointer, config, metadata, optimized)
        if content is not None:
            return content
        content = self.transport.download(pointer["file_id"])
        self._cache_content(root, path, metadata, content, config, optimized)
        return content

    def retain(self, config):
        """Make active bytes durable before desired startup or cleanup can retire them."""
        try:
            validate_soundbank_cloud_metadata(config)
            self._check_runtime_root(resolve_soundbank_root(config.get("static_soundbank", {})))
            for root, filename, path, metadata, optimized in _assets(config, pointer_only=True):
                self._obtain(root, filename, path, metadata, optimized, config)
        except (ConfigUnavailable, OSError, ValueError, TypeError, KeyError, SoundbankError):
            raise ConfigUnavailable("Active cloud soundbank assets unavailable; runtime was not applied") from None

    @staticmethod
    def _publication_plan(updated, node_id, repo_defaults):
        layers = updated["layers"]
        if not centralized(updated):
            sources = [None, layers["defaults"], layers["overrides"]]
            baseline = {}
        else:
            node = layers["nodes"][node_id]
            global_layer = layers["global"]
            if legacy_centralized(updated):
                sources = [None, global_layer["defaults"], global_layer["overrides"]]
                baseline = {}
            else:
                sources = [None, global_layer]
                baseline = repo_defaults
            for name, scope in (("environment", "environments"), ("role", "roles")):
                if node[name] is not None:
                    sources.append(layers[scope][node[name]])
            sources.append(node["overrides"])

        def stamp(value, owner):
            # Scalar replacement and recursive map merges mirror merge_configs.
            if isinstance(value, Mapping):
                return {key: stamp(child, owner) for key, child in value.items()}
            return owner

        origins = stamp(baseline, 0)
        for owner, source in enumerate(sources[1:], 1):
            origins = merge_configs(origins, stamp(source, owner))
        defaults, overrides = resolve_layers(updated, node_id, repo_defaults)
        effective = merge_configs(defaults, overrides)
        destinations = []
        for phrase, entry in effective.get("static_soundbank", {}).get("entries", {}).items():
            origin = origins["static_soundbank"]["entries"][phrase]
            canonical_owner = origin if isinstance(origin, int) else origin.get("file", 0)
            owners = [(False, canonical_owner)]
            if soundbank_entry_optimized(entry) is not None:
                owners.append((True, origin.get("optimized", {}).get("file", 0)))
            for optimized, owner in owners:
                destinations.append((phrase, optimized, owner))
        return sources, destinations, effective

    def provisioning_overrides(self, obj, node_id, repo_defaults):
        """Explicitly own only repo-default files, leaving cloud file owners intact."""
        updated = copy.deepcopy(obj)
        _, destinations, effective = self._publication_plan(updated, node_id, repo_defaults)
        layer = (updated["layers"]["nodes"][node_id]["overrides"] if centralized(updated)
                 else updated["layers"]["overrides"])
        for phrase, optimized, owner in destinations:
            if owner:
                continue
            entries = layer.setdefault("static_soundbank", {}).setdefault("entries", {})
            entry = entries.setdefault(phrase, {})
            source = effective["static_soundbank"]["entries"][phrase]
            if optimized:
                entry.setdefault("optimized", {})["file"] = soundbank_entry_filename(source["optimized"])
            else:
                entry["file"] = soundbank_entry_filename(source)
        return layer

    @staticmethod
    def _known_pointers(*configs):
        known = {}
        for config in configs:
            for _, _, _, metadata, _ in _assets(config, pointer_only=True):
                pointer = metadata["cloud"]
                validate_soundbank_cloud_pointer(pointer)
                known[(pointer["sha256"], pointer["size"])] = dict(pointer)
        return known

    def _validated_local_assets(self, config):
        validate_soundbank_cloud_metadata(config)
        self._check_runtime_root(resolve_soundbank_root(config.get("static_soundbank", {})))
        pending = []
        for root, filename, _, metadata, optimized in _assets(config):
            path = resolve_soundbank_asset(root, filename, require_file=True)
            content = path.read_bytes()
            if not content:
                raise ValueError("Cloud soundbank asset is empty")
            _validate_p3(content, path, config, metadata, optimized)
            pending.append((root, path, metadata, optimized, content, (_digest(content), len(content))))
        return pending

    def validate_local(self, config, current_config=None):
        """Read-only provisioning preflight, including every pointer we may reuse."""
        pending = self._validated_local_assets(config)
        if current_config is not None:
            known = self._known_pointers(current_config, config)
            for _, path, metadata, optimized, _, identity in pending:
                pointer = known.get(identity)
                if pointer is not None:
                    content = self.transport.download(pointer["file_id"])
                    if not _matches(content, pointer):
                        raise ValueError("Existing soundbank pointer verification failed")
                    _validate_p3(content, path, config, metadata, optimized)

    def verify_remote(self, config, *, compare_local=False):
        """Verify complete cloud assets without caches or runtime materialization."""
        validate_soundbank_cloud_metadata(config)
        for root, filename, path, metadata, optimized in _assets(config):
            pointer = metadata.get("cloud")
            validate_soundbank_cloud_pointer(pointer)
            content = self.transport.download(pointer["file_id"])
            if not _matches(content, pointer):
                raise ValueError("Remote soundbank verification failed")
            _validate_p3(content, path, config, metadata, optimized)
            if compare_local:
                local = resolve_soundbank_asset(root, filename, require_file=True).read_bytes()
                if content != local:
                    raise ValueError("Soundbank source changed during provisioning")

    def publish_layers(self, obj, node_id, repo_defaults, current_config):
        """Put storage metadata at each effective file's owner, never pin inheritance."""
        try:
            updated = copy.deepcopy(obj)
            sources, destinations, effective = self._publication_plan(updated, node_id, repo_defaults)
            if any(not owner for _, _, owner in destinations):
                raise ValueError("Repo-default soundbank assets need an explicit cloud file override")
            published = self.publish(effective, current_config)
            for phrase, optimized, owner in destinations:
                entries = sources[owner]["static_soundbank"]["entries"]
                if isinstance(entries[phrase], str):
                    entries[phrase] = {"file": entries[phrase]}
                target = entries[phrase]["optimized"] if optimized else entries[phrase]
                published_entry = published["static_soundbank"]["entries"][phrase]
                pointer = (published_entry["optimized"] if optimized else published_entry)["cloud"]
                target["cloud"] = copy.deepcopy(pointer)
                # Narrower metadata must not shadow the pointer at the file owner.
                for source in sources[owner + 1:]:
                    entry = source.get("static_soundbank", {}).get("entries", {}).get(phrase)
                    metadata = soundbank_entry_optimized(entry) if optimized else entry
                    if isinstance(metadata, Mapping):
                        metadata.pop("cloud", None)
            return updated
        except (ConfigUnavailable, OSError, ValueError, TypeError, KeyError, SoundbankError):
            raise ConfigUnavailable("Cloud soundbank assets unavailable; configuration was not published") from None

    def publish(self, config, current_config):
        """Cloudify every retained reference, without modifying local/runtime files."""
        try:
            candidate = copy.deepcopy(config)
            validate_soundbank_cloud_metadata(candidate)
            self._check_runtime_root(resolve_soundbank_root(candidate.get("static_soundbank", {})))
            entries = candidate.get("static_soundbank", {}).get("entries", {})
            # Legacy strings gain only the optional metadata object on cloud Save.
            for phrase, entry in list(entries.items()):
                if isinstance(entry, str):
                    entries[phrase] = {"file": soundbank_entry_filename(entry)}
            known = self._known_pointers(current_config, candidate)
            # Validate all local references before uploading any of the transaction.
            pending = self._validated_local_assets(candidate)
            for root, path, metadata, optimized, content, identity in pending:
                pointer = known.get(identity)
                if pointer is None:
                    suffix = path.suffix.lower()
                    file_id = self.transport.upload_blob(
                        self.folder_id, content, f"soundbank-{identity[0]}{suffix}",
                        SOUNDBANK_MIME_TYPES[suffix],
                    )
                    pointer = {"file_id": file_id, "sha256": identity[0], "size": identity[1]}
                    validate_soundbank_cloud_pointer(pointer)
                    if not _matches(self.transport.download(file_id), pointer):
                        raise ValueError("Soundbank upload verification failed")
                    known[identity] = pointer
                metadata["cloud"] = dict(pointer)
                self._cache_content(root, path, metadata, content, candidate, optimized)
            return candidate
        except (ConfigUnavailable, OSError, ValueError, TypeError, KeyError, SoundbankError):
            raise ConfigUnavailable("Cloud soundbank assets unavailable; configuration was not published") from None

    def materialize(self, config):
        """Stage and verify all required downloads before atomic local publication."""
        staged = []
        try:
            validate_soundbank_cloud_metadata(config)
            self._check_runtime_root(resolve_soundbank_root(config.get("static_soundbank", {})))
            assets = {}
            for root, filename, path, metadata, optimized in _assets(config, pointer_only=True):
                pointer = metadata["cloud"]
                target = _safe_target(root, filename)
                previous = assets.get(target)
                if previous and (previous[3]["cloud"]["sha256"], previous[3]["cloud"]["size"]) != (
                    pointer["sha256"], pointer["size"]
                ):
                    raise ValueError("Conflicting cloud pointers for one local asset")
                # Validate all contracts, including shared filenames, before use.
                sample_rate = soundbank_p3_sample_rate(config, metadata if optimized else None) if target.suffix.lower() == ".p3" else None
                if previous and previous[5] != sample_rate:
                    raise ValueError("Conflicting P3 audio contracts for one local asset")
                assets[target] = (root, filename, target, metadata, optimized, sample_rate)
            for root, filename, target, metadata, optimized, _ in assets.values():
                pointer = metadata["cloud"]
                content = self._obtain(root, filename, target, metadata, optimized, config)
                if self._read_verified(target, pointer, config, metadata, optimized) is not None:
                    continue
                target = _safe_target(root, filename, create=True)
                temporary = self._stage(content, target)
                staged.append((temporary, root, filename, target))
                staged_content = temporary.read_bytes()
                if not _matches(staged_content, pointer):
                    raise ValueError("Cloud soundbank download verification failed")
                _validate_p3(staged_content, target, config, metadata, optimized)
            for temporary, root, filename, target in staged:
                _replace_staged(temporary, root, filename, target)
        except (ConfigUnavailable, OSError, ValueError, TypeError, KeyError, SoundbankError):
            raise ConfigUnavailable("Cloud soundbank materialization unavailable; runtime was not applied") from None
        finally:
            for temporary, _, _, _ in staged:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    # Preserve the sanitized failure; a locked temp file is
                    # hidden and never referenced by runtime or cleanup.
                    pass
