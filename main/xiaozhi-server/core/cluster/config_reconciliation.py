"""One process owns bounded NATS hints and serialized Cloud desired refreshes."""

import asyncio
import copy
import logging
from datetime import datetime, timezone

from config.cloud_layers import centralized, shared_cluster
from config.config_loader import merge_configs
from config.config_store import ConfigUnavailable
from .config_protocol import (
    CONFIG_CHANGED_SUBJECT, MAX_EVENT_BYTES, config_changed, parse_config_changed,
)
from .soundbank_reconciliation import SoundbankReconciliation

LOGGER = logging.getLogger("xiaozhi.control_plane")
CONTROL_PROTOCOL = "xiaozhi-control-plane-v1"
CAPABILITIES = {
    "configuration": True, "config_sync": True, "cluster_status": True,
    "migration": True, "runtime_status": False, "logs": False,
    "restart": False, "rolling_restart": False, "source_switch": False,
    "push_tts": False, "memory": False, "soundbank_authoring": False,
    "soundbank_preview": False, "vip_assignment": False,
    "soundbank_sync": True,
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def safe_source(status):
    source = {key: copy.deepcopy(status.get(key)) for key in (
        "config_provider", "node_id", "desired_revision", "active_revision",
        "sync_state", "sync_status", "restart_required", "conflict", "pending_provider",
        "environment", "role", "active_environment", "active_role", "schema_version",
        "settings_scope", "cluster_migration_required", "last_sync",
    )}
    source.update(runtime_revision=None,
                  last_error="Cloud synchronization needs attention" if status.get("last_error") else None,
                  source="Shared Cloud desired configuration",
                  active_source="Last recorded conversation startup", cache_path="Node-local desired cache")
    return source


def _client_factory():
    # Imports remain lightweight, and tests inject a transport without sockets.
    from nats.aio.client import Client
    return Client()


class ConfigReconciliation:
    def __init__(self, store, config, *, client_factory=_client_factory):
        self.store, self.config = store, config
        self.client_factory = client_factory
        self.client = None
        self.memory = None
        self.nats_state = "not_started"
        self.reconciliation = {
            "state": "not_started", "last_attempt_at": None,
            "last_success_at": None, "last_error_at": None, "error_code": None,
        }
        self.hint_publication = {"state": "not_started", "last_error_at": None}
        self.healthy = False
        self.http_operational = False
        self.source = {"config_provider": "google_drive", "node_id": store.bootstrap["node_id"],
                       "desired_revision": None, "active_revision": None,
                       "runtime_revision": None, "restart_required": True,
                       "sync_state": "unavailable", "sync_status": "not_synced"}
        self.vip = None
        self.bootstrap_context = None
        self._operation_lock = asyncio.Lock()
        self._wake, self._stop, self._closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self._pending_revision = 0
        self._highest_hint_revision = 0
        self._hints = asyncio.Queue(maxsize=64)
        self._tasks = []
        self._loop = None
        self._observer = self._committed
        self._previous_observer = None
        self.soundbank = SoundbankReconciliation(store, config.reconcile_interval)

    def _capture(self):
        """Capture safe cached status off-loop; health/status requests do no I/O."""
        try:
            with self.store.locked():
                status = self.store.status_unlocked()
                self.source = safe_source(status)
                # This process never owns a conversation runtime. Disk active is
                # last successful app startup, not proof that it is running now.
                snapshot = self.store.desired_snapshot
                obj = snapshot["payload"]["object"] if snapshot else None
                self.healthy = bool(obj and centralized(obj)
                                    and self.store.bootstrap["node_id"] in obj["layers"]["nodes"])
                effective = merge_configs(self.store.defaults_unlocked(), self.store.read_unlocked())
                self.vip = effective.get("cluster", {}).get("ingress", {}).get("vip")
                # Publish one immutable, non-secret view for bootstrap requests.
                # They must not queue behind Drive refreshes or resolve layers again.
                self.bootstrap_context = (
                    self.vip, effective.get("server", {}).get("timezone_offset", 0)
                ) if self.healthy else None
                if self._loop is not None and self.memory is not None:
                    self._loop.call_soon_threadsafe(self.memory.request_refresh)
                if self._loop is not None:
                    if obj and shared_cluster(obj):
                        self._loop.call_soon_threadsafe(self.soundbank.request,
                            snapshot["payload"]["manifest"]["revision"], effective)
                    else:
                        self._loop.call_soon_threadsafe(self.soundbank.legacy)
        except (OSError, ValueError, TypeError, KeyError):
            self.healthy = False
            self.vip = None
            self.bootstrap_context = None

    async def operation(self, function, *args, **kwargs):
        """Serialize control-plane edits/reads/refreshes and keep worker threads owned."""
        async with self._operation_lock:
            if self._stop.is_set():
                raise ConfigUnavailable("Control plane is stopping")
            def execute():
                try:
                    return function(*args, **kwargs)
                finally:
                    self._capture()
            # Shield the blocking transaction from a disconnected HTTP request.
            # Cancellation waits for it: no thread can write after service stop.
            task = asyncio.create_task(asyncio.to_thread(execute))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # Repeated request cancellation must not abandon the transaction.
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not task.cancelled():
                    task.exception()
                raise

    def _refresh(self):
        with self.store.locked():
            # Observe app.py's active file; never select/mark/apply runtime here.
            try:
                active = self.store._read_cache("active.json")
                previous = self.store.active_snapshot
                if previous is None or (active["payload"]["manifest"]["revision"] >=
                                        previous["payload"]["manifest"]["revision"]):
                    self.store.active_snapshot = active
                    self.store.active_revision = active["payload"]["manifest"]["revision"]
            except (OSError, ValueError, TypeError, KeyError):
                pass
            self.store.refresh_unlocked(strict=True)

    async def reconcile(self):
        self.reconciliation.update(state="refreshing", last_attempt_at=_now())
        try:
            await self.operation(self._refresh)
        except Exception:
            self.reconciliation.update(state="error", last_error_at=_now(), error_code="cloud_refresh_failed")
            LOGGER.warning("Cloud config reconciliation unavailable; retaining validated desired state")
            return False
        self.reconciliation.update(state="ready", last_success_at=_now(), error_code=None)
        return True

    async def _on_hint(self, message):
        if self._stop.is_set():
            return
        revision = parse_config_changed(message.data)
        current = self.source.get("desired_revision") or 0
        if revision is None or revision <= current or revision <= self._highest_hint_revision:
            return
        self._highest_hint_revision = revision
        self._pending_revision = revision
        self._wake.set()

    async def _reconcile_loop(self):
        while not self._stop.is_set():
            periodic = False
            try:
                await asyncio.wait_for(self._wake.wait(), self.config.reconcile_interval)
            except asyncio.TimeoutError:
                periodic = True
            self._wake.clear()
            target, self._pending_revision = self._pending_revision, 0
            if self._stop.is_set():
                break
            if periodic or target > (self.source.get("desired_revision") or 0):
                await self.reconcile()

    def _committed(self, revision):
        """Store callback runs in the CAS worker thread; only metadata crosses."""
        if self._loop is not None and not self._stop.is_set():
            self._loop.call_soon_threadsafe(self._enqueue_hint, revision)

    def _enqueue_hint(self, revision):
        if self._stop.is_set():
            return
        try:
            self._hints.put_nowait(config_changed(revision))
        except (asyncio.QueueFull, ValueError):
            self.hint_publication.update(state="dropped", last_error_at=_now())
            LOGGER.warning("Committed config hint dropped; periodic reconciliation will recover")

    async def _publish_loop(self):
        while True:
            data = await self._hints.get()
            try:
                if self.client is None or not self.client.is_connected:
                    raise ConnectionError
                await asyncio.wait_for(self.client.publish(CONFIG_CHANGED_SUBJECT, data), timeout=2)
                # Flush is best effort too: enqueueing alone does not prove delivery.
                await asyncio.wait_for(self.client.flush(timeout=2), timeout=3)
                self.hint_publication["state"] = "sent"
            except Exception:
                self.hint_publication.update(state="failed", last_error_at=_now())
                LOGGER.warning("Committed config hint unavailable; Cloud save remains authoritative")
            finally:
                self._hints.task_done()

    async def _disconnected(self):
        self.nats_state = "disconnected"
        LOGGER.warning("Control-plane NATS disconnected")

    async def _reconnected(self):
        self.nats_state = "connected"
        # Heal missed events immediately as well as at the periodic deadline.
        self._pending_revision = max(self._pending_revision, (self.source.get("desired_revision") or 0) + 1)
        self._wake.set()
        if self.memory is not None:
            self.memory.request_refresh()
        LOGGER.info("Control-plane NATS reconnected")

    async def _closed_callback(self):
        self.nats_state = "closed"
        self._closed.set()

    async def _error(self, error):
        LOGGER.warning("Control-plane NATS operation unavailable")

    async def _close_client(self, client):
        if client is None or client.is_closed:
            return
        if self._stop.is_set() and client.is_connected:
            try:
                await asyncio.wait_for(client.drain(), timeout=10)
            except Exception:
                LOGGER.warning("Control-plane NATS drain unavailable")
        if not client.is_closed:
            try:
                await asyncio.wait_for(client.close(), timeout=5)
            except Exception:
                LOGGER.warning("Control-plane NATS close unavailable")

    async def _connect_loop(self):
        while not self._stop.is_set():
            self.nats_state = "connecting"
            self._closed.clear()
            client = None
            try:
                client = self.client_factory()
                self.client = client
                credentials = self.config.nats
                await client.connect(
                    servers=list(credentials.servers), user=credentials.username, password=credentials.password,
                    name="xiaozhi-control-plane", allow_reconnect=True, max_reconnect_attempts=-1,
                    reconnect_time_wait=2, connect_timeout=5, ping_interval=20, max_outstanding_pings=2,
                    drain_timeout=10, pending_size=64 * 1024, flush_timeout=2,
                    error_cb=self._error, disconnected_cb=self._disconnected,
                    reconnected_cb=self._reconnected, closed_cb=self._closed_callback,
                )
                # Broadcast subscription: intentionally no queue group.
                await client.subscribe(CONFIG_CHANGED_SUBJECT, cb=self._on_hint,
                    pending_msgs_limit=64, pending_bytes_limit=64 * MAX_EVENT_BYTES)
                if self.memory is not None:
                    await self.memory.register(client)
                self.nats_state = "connected"
                LOGGER.info("Control-plane NATS connected")
                await self._closed.wait()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.nats_state = "unavailable"
                LOGGER.warning("Control-plane NATS connection unavailable; retrying")
            finally:
                await self._close_client(client)
            if not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=2)
                except asyncio.TimeoutError:
                    pass

    async def start(self):
        self._loop = asyncio.get_running_loop()
        await self.reconcile()  # Live Cloud read before HTTP becomes operational.
        self.soundbank.start()
        if self.memory is not None:
            self.memory.start()
        self._previous_observer = self.store.publication_observer
        self.store.publication_observer = self._observer
        self._tasks = [asyncio.create_task(function(), name=name) for function, name in (
            (self._reconcile_loop, "config-reconciliation"), (self._connect_loop, "config-nats"),
            (self._publish_loop, "config-hints"),
        )]

    async def stop(self):
        self.http_operational = False
        self._stop.set()
        self.soundbank.begin_stop()
        self._wake.set()
        if self.store.publication_observer is self._observer:
            self.store.publication_observer = self._previous_observer
        if self._tasks:
            # Reconciliation completes its owned disk/network transaction first.
            self._tasks[1].cancel()
            self._tasks[2].cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
        # Also wait for an HTTP operation already executing off-loop.
        async with self._operation_lock:
            pass
        await self.soundbank.stop()
        if self.memory is not None:
            await self.memory.stop()
        self.nats_state = "closed"

    def status(self):
        return {
            "protocol": CONTROL_PROTOCOL, "capability_version": 1,
            "node_id": self.store.bootstrap["node_id"],
            "configuration": copy.deepcopy(self.source),
            "ingress": {"configured_vip": self.vip, "state": "desired_only"},
            "nats": {"state": self.nats_state},
            "reconciliation": copy.deepcopy(self.reconciliation),
            "hint_publication": copy.deepcopy(self.hint_publication),
            "soundbank": copy.deepcopy(self.soundbank.status),
            "memory": self.memory.status() if self.memory is not None else {"state":"disabled"},
            "capabilities": copy.deepcopy(CAPABILITIES),
        }
