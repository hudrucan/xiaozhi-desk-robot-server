"""Background verified Soundbank caches; no runtime selection or provider loading."""

import asyncio
import copy
import logging
import os
import stat
import threading
from datetime import datetime, timezone

from config.cloud_soundbank import CloudSoundbankAssets, _assets, _validate_p3
from config.config_store import ConfigUnavailable, LocalConfigStore, canonical_bytes, checksum
from config.drive_transport import GoogleDriveTransport
from core.soundbank import validate_soundbank_cloud_metadata

LOGGER = logging.getLogger("xiaozhi.control_plane")
PROTOCOL = "xiaozhi-soundbank-cache-v1"
MAX_ASSETS = 4096
MAX_ASSET_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
STATES = {"not_started", "legacy", "pending", "syncing", "ready", "disabled", "error"}
ERRORS = {None, "soundbank_sync_failed"}


def now():
    return datetime.now(timezone.utc).isoformat()


class BoundedAssets:
    """Own a separate Drive HTTP session from the serialized Config store."""
    def __init__(self, transport):
        self.transport = transport

    def download(self, file_id):
        if isinstance(self.transport, GoogleDriveTransport):
            return self.transport.download_limited(file_id, MAX_ASSET_BYTES)
        # Injected in-memory transports use the same post-download size check.
        content = self.transport.download(file_id)
        if len(content) > MAX_ASSET_BYTES:
            raise ConfigUnavailable("Cloud asset exceeds the local sync limit")
        return content

    def close(self):
        if isinstance(self.transport, GoogleDriveTransport) and self.transport.session is not None:
            self.transport.session.close()


class VerifiedCacheAssets(CloudSoundbankAssets):
    @staticmethod
    def _read_verified(path, pointer, config, metadata, optimized):
        # Local corruption must not turn a small expected blob into an unbounded
        # read or a blocking FIFO. Cache writes retain the existing path guards.
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != pointer["size"]:
                return None
            content = stream.read(min(pointer["size"], MAX_ASSET_BYTES) + 1)
        if len(content) != pointer["size"] or checksum(content) != pointer["sha256"]:
            return None
        _validate_p3(content, path, config, metadata, optimized)
        return content


class SoundbankReconciliation:
    def __init__(self, store, interval):
        self.node = store.bootstrap["node_id"]
        transport = store.transport
        if isinstance(transport, GoogleDriveTransport):
            transport = GoogleDriveTransport(transport.credentials_path)
        self.transport = BoundedAssets(transport)
        self.assets = VerifiedCacheAssets(self.transport, store.folder_id, store.soundbank_assets.cache_dir)
        self.index = self.assets.cache_dir.parent / "ready.json"
        self.io = LocalConfigStore(self.index)
        self.interval = interval
        self.wake = asyncio.Event()
        self.stopping = False
        self.target = None
        self.lock = threading.Lock()
        self.task = None
        self.status = {"state": "not_started", "desired_revision": None, "synced_revision": None,
                       "expected_assets": 0, "verified_assets": 0, "last_success_at": None,
                       "last_error_at": None, "error_code": None}

    def request(self, revision, config):
        # Only immutable, validated desired metadata enters this worker. Never
        # pass API keys, provider configuration or active-runtime state.
        subset = {"static_soundbank": copy.deepcopy(config.get("static_soundbank", {})),
                  "xiaozhi": {"audio_params": copy.deepcopy(config.get("xiaozhi", {}).get("audio_params", {}))}}
        identity = (revision, checksum(canonical_bytes(subset)))
        with self.lock:
            if self.stopping or (self.target is not None and self.target[0] == identity):
                return
            self.target = (identity, subset)
        entries = subset["static_soundbank"].get("entries", {})
        count = sum(1 + int(isinstance(entry, dict) and isinstance(entry.get("optimized"), dict))
                    for entry in entries.values())
        self.status.update(state="pending", desired_revision=revision, expected_assets=count,
                           verified_assets=0, error_code=None)
        self.wake.set()

    def legacy(self):
        with self.lock:
            self.target = None
        self.status.update(state="legacy", desired_revision=None, expected_assets=0,
                           verified_assets=0, error_code=None)

    def _prepare(self, target):
        identity, config = target
        validate_soundbank_cloud_metadata(config)
        enabled = bool(config["static_soundbank"].get("enabled"))
        plan = list(_assets(config))
        if len(plan) > MAX_ASSETS:
            raise ValueError("Soundbank sync exceeds the asset limit")
        pointers = [item for item in plan if "cloud" in item[3]]
        if enabled and len(pointers) != len(plan):
            raise ValueError("Enabled shared Soundbank requires published Cloud pointers")
        if any(item[3]["cloud"]["size"] > MAX_ASSET_BYTES for item in pointers) or sum(
                item[3]["cloud"]["size"] for item in pointers) > MAX_TOTAL_BYTES:
            raise ValueError("Soundbank sync exceeds the byte limit")
        # Content-addressed caches retain old generations. A failure may leave
        # verified new blobs, but never replaces the last complete ready index.
        for root, filename, path, metadata, optimized in pointers:
            with self.lock:
                if self.stopping or self.target is None or self.target[0] != identity:
                    return None
            self.assets._obtain(root, filename, path, metadata, optimized, config)
        index = {"protocol": PROTOCOL, "node_id": self.node, "revision": identity[0],
                 "fingerprint": identity[1], "configuration": config, "assets": len(pointers)}
        with self.lock:
            if self.stopping or self.target is None or self.target[0] != identity:
                return None
            # Never rewrite runtime Soundbank paths or the Config active cache.
            # Empty/disabled banks do not create this directory via blob writes.
            self.index.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.io._atomic_bytes(self.index, canonical_bytes(index))
        return len(pointers), enabled

    async def run(self):
        try:
            while not self.stopping:
                try:
                    await asyncio.wait_for(self.wake.wait(), self.interval)
                except asyncio.TimeoutError:
                    pass
                self.wake.clear()
                with self.lock:
                    target = self.target
                if self.stopping:
                    break
                if target is None:
                    continue
                self.status.update(state="syncing", verified_assets=0, error_code=None)
                try:
                    # Stop joins this owned thread; downloads have bounded sizes
                    # and transport timeouts. HTTP edits never await asset I/O.
                    result = await asyncio.to_thread(self._prepare, target)
                except Exception:
                    with self.lock:
                        current = self.target is not None and self.target[0] == target[0]
                    if current:
                        self.status.update(state="error", last_error_at=now(), error_code="soundbank_sync_failed")
                        LOGGER.warning("Soundbank cache sync unavailable; retaining last complete assets")
                else:
                    with self.lock:
                        current = self.target is not None and self.target[0] == target[0]
                    if result is not None and current:
                        count, enabled = result
                        self.status.update(state="ready" if enabled else "disabled", synced_revision=target[0][0],
                                           expected_assets=count, verified_assets=count,
                                           last_success_at=now(), error_code=None)
        finally:
            self.transport.close()

    def start(self):
        self.task = asyncio.create_task(self.run(), name="soundbank-reconciliation")

    def begin_stop(self):
        with self.lock:
            self.stopping = True
        self.wake.set()

    async def stop(self):
        self.begin_stop()
        if self.task is not None:
            # Do not cancel to_thread: a cancelled await would abandon disk I/O.
            try:
                await asyncio.shield(self.task)
            except asyncio.CancelledError:
                while not self.task.done():
                    try:
                        await asyncio.shield(self.task)
                    except asyncio.CancelledError:
                        continue
                raise
            self.task = None


def safe_status(value):
    """Validate the fixed public peer schema; never forward arbitrary peer fields."""
    fields = {"state", "desired_revision", "synced_revision", "expected_assets", "verified_assets",
              "last_success_at", "last_error_at", "error_code"}
    if not isinstance(value, dict) or not fields <= value.keys():
        raise ValueError("Invalid Soundbank status")
    result = {key: value[key] for key in fields}
    if result["state"] not in STATES or result["error_code"] not in ERRORS:
        raise ValueError("Invalid Soundbank status state")
    for key in ("desired_revision", "synced_revision"):
        if result[key] is not None and (type(result[key]) is not int or result[key] < 1):
            raise ValueError("Invalid Soundbank revision")
    for key in ("expected_assets", "verified_assets"):
        if type(result[key]) is not int or not 0 <= result[key] <= MAX_ASSETS:
            raise ValueError("Invalid Soundbank count")
    for key in ("last_success_at", "last_error_at"):
        if result[key] is not None:
            if not isinstance(result[key], str) or len(result[key]) > 40:
                raise ValueError("Invalid Soundbank timestamp")
            datetime.fromisoformat(result[key])
    return result
