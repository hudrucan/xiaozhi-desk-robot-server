import copy
import threading
import time
from collections import deque
from datetime import datetime, timezone


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _percentile(values, percentile):
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * percentile)))
    return round(ordered[index], 1)


class RuntimeDiagnostics:
    """Keep a bounded, process-local view of connections and completed turns."""

    def __init__(self, turn_limit=50):
        self._lock = threading.Lock()
        self._connections = {}
        self._devices = {}
        self._vision_requests = {}
        self._turns = deque(maxlen=max(1, int(turn_limit)))
        self._device_events = deque(maxlen=30)

    def register_connection(self, session_id, device_id=None, client_ip=None):
        with self._lock:
            self._connections[session_id] = {
                "session_id": session_id,
                "device_id": device_id,
                "client_ip": client_ip,
                "connected_at": _utc_now(),
                "active_turn": None,
            }

    def unregister_connection(self, session_id):
        with self._lock:
            self._connections.pop(session_id, None)

    def begin_turn(self, session_id, turn_id, source):
        with self._lock:
            connection = self._connections.get(session_id)
            if connection is not None:
                connection["active_turn"] = {
                    "turn_id": turn_id,
                    "source": source,
                    "started_at": _utc_now(),
                }

    def complete_turn(self, metrics):
        turn = copy.deepcopy(metrics)
        turn["completed_at"] = _utc_now()
        with self._lock:
            self._turns.append(turn)
            connection = self._connections.get(turn.get("session_id"))
            if connection is not None:
                connection["active_turn"] = None

    def record_vision_request(
        self,
        device_id,
        request_id,
        outcome,
        duration_ms,
        image_bytes=None,
        response_bytes=None,
    ):
        if not device_id:
            return
        with self._lock:
            self._vision_requests.pop(device_id, None)
            self._vision_requests[device_id] = {
                "request_id": request_id,
                "outcome": outcome,
                "duration_ms": duration_ms,
                "image_bytes": image_bytes,
                "response_bytes": response_bytes,
                "completed_at": _utc_now(),
                "_completed_monotonic": time.monotonic(),
            }
            while len(self._vision_requests) > 32:
                self._vision_requests.pop(next(iter(self._vision_requests)))

    def record_bootstrap(
        self,
        device_id,
        client_id=None,
        firmware_version=None,
        device_model=None,
        reset_reason=None,
    ):
        """Remember device metadata and flag bootstrap over an active socket."""
        with self._lock:
            if device_id:
                self._devices.pop(device_id, None)
                self._devices[device_id] = {
                    "device_id": device_id,
                    "client_id": client_id,
                    "firmware_version": firmware_version,
                    "device_model": device_model,
                    "reset_reason": reset_reason,
                    "last_bootstrap_at": _utc_now(),
                }
                while len(self._devices) > 32:
                    self._devices.pop(next(iter(self._devices)))
            existing = next(
                (
                    connection
                    for connection in self._connections.values()
                    if device_id and connection.get("device_id") == device_id
                ),
                None,
            )
            if existing is None:
                return None
            recent_vision = self._vision_requests.get(device_id)
            if recent_vision is not None:
                since_vision_ms = round(
                    (time.monotonic() - recent_vision["_completed_monotonic"])
                    * 1000,
                    1,
                )
                if since_vision_ms <= 120000:
                    recent_vision = {
                        key: value
                        for key, value in recent_vision.items()
                        if not key.startswith("_")
                    }
                    recent_vision["before_bootstrap_ms"] = since_vision_ms
                else:
                    recent_vision = None
            event = {
                "type": "unexpected_bootstrap",
                "severity": "warning",
                "observed_at": _utc_now(),
                "device_id": device_id,
                "client_id": client_id,
                "firmware_version": firmware_version,
                "device_model": device_model,
                "reset_reason": reset_reason,
                "previous_session_id": existing.get("session_id"),
                "active_turn": copy.deepcopy(existing.get("active_turn")),
                "recent_vision": copy.deepcopy(recent_vision),
            }
            self._device_events.append(event)
            return copy.deepcopy(event)

    def snapshot(self):
        with self._lock:
            connections = copy.deepcopy(list(self._connections.values()))
            turns = copy.deepcopy(list(reversed(self._turns)))
            device_events = copy.deepcopy(list(reversed(self._device_events)))
            devices = copy.deepcopy(list(self._devices.values()))

        recent = turns[:20]
        durations = [
            float(turn["total_ms"])
            for turn in recent
            if isinstance(turn.get("total_ms"), (int, float))
        ]
        completed = sum(turn.get("outcome") == "completed" for turn in recent)
        return {
            "sampled_at": _utc_now(),
            "connections": {
                "active_count": len(connections),
                "items": connections,
            },
            "turns": turns,
            "device_events": device_events,
            "devices": devices,
            "summary": {
                "sample_size": len(recent),
                "completed": completed,
                "attention": len(recent) - completed,
                "median_total_ms": _percentile(durations, 0.5),
                "p95_total_ms": _percentile(durations, 0.95),
            },
        }


runtime_diagnostics = RuntimeDiagnostics()
