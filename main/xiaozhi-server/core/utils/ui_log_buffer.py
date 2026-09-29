import re
import threading
import uuid
from collections import deque
from datetime import datetime, timezone


MAX_BUFFER_BYTES = 2 * 1024 * 1024
MAX_BUFFER_LINES = 2000
MAX_LINE_BYTES = 8 * 1024
MAX_API_LIMIT = 500

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_BEARER_SECRET = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_NAMED_SECRET = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|authorization|password|"
    r"private[_-]?key|secret(?:[_-]?key)?|token)\b\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)"
)
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:access_token|api_key|auth|key|secret|token)=)[^&#\s]+"
)
_LONG_BLOB = re.compile(r"[A-Za-z0-9+/=_-]{512,}")


def _utc_timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _truncate_utf8(value, max_bytes):
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return value
    suffix = " … [truncated]"
    suffix_bytes = suffix.encode("utf-8")
    available = max(0, max_bytes - len(suffix_bytes))
    return encoded[:available].decode("utf-8", errors="ignore") + suffix


def _redact(value):
    value = _ANSI_ESCAPE.sub("", str(value))
    value = _BEARER_SECRET.sub("Bearer [redacted]", value)
    value = _NAMED_SECRET.sub(r"\1[redacted]", value)
    value = _QUERY_SECRET.sub(r"\1[redacted]", value)
    return _LONG_BLOB.sub("[binary omitted]", value)


class UiLogBuffer:
    """Bounded current-process log buffer for the Settings UI."""

    def __init__(self):
        self.run_id = uuid.uuid4().hex
        self.started_at = _utc_timestamp()
        self._entries = deque()
        self._total_bytes = 0
        self._next_sequence = 1
        self._dropped_total = 0
        self._lock = threading.Lock()

    def append(self, source, level, message, tag="", timestamp=None):
        safe_source = str(source or "server")[:32]
        safe_level = str(level or "INFO").upper()[:16]
        safe_tag = _truncate_utf8(_redact(tag or ""), 512)
        safe_message = _redact(message)
        lines = safe_message.splitlines() or [""]
        created_at = timestamp or _utc_timestamp()

        with self._lock:
            for line in lines:
                text = _truncate_utf8(line, MAX_LINE_BYTES)
                size = (
                    len(text.encode("utf-8"))
                    + len(safe_tag.encode("utf-8"))
                    + 96
                )
                entry = {
                    "sequence": self._next_sequence,
                    "timestamp": created_at,
                    "source": safe_source,
                    "level": safe_level,
                    "tag": safe_tag,
                    "message": text,
                    "size": size,
                }
                self._next_sequence += 1
                self._entries.append(entry)
                self._total_bytes += size
                self._trim_locked()

    def append_loguru(self, message):
        record = message.record
        extra = record.get("extra") or {}
        timestamp = record["time"].astimezone(timezone.utc).isoformat(
            timespec="milliseconds"
        )
        self.append(
            "server",
            record["level"].name,
            record["message"],
            tag=extra.get("tag") or record.get("name", ""),
            timestamp=timestamp,
        )

    def clear_source(self, source):
        source = str(source)
        with self._lock:
            retained = deque(
                entry for entry in self._entries if entry["source"] != source
            )
            removed = len(self._entries) - len(retained)
            if removed:
                self._entries = retained
                self._total_bytes = sum(entry["size"] for entry in retained)

    def snapshot(self, after=0, limit=300):
        try:
            after = max(0, int(after))
        except (TypeError, ValueError):
            after = 0
        try:
            limit = min(MAX_API_LIMIT, max(1, int(limit)))
        except (TypeError, ValueError):
            limit = 300

        with self._lock:
            entries = list(self._entries)
            oldest_sequence = entries[0]["sequence"] if entries else 0
            latest_sequence = (
                entries[-1]["sequence"] if entries else self._next_sequence - 1
            )
            cursor_reset = bool(
                after and oldest_sequence and after < oldest_sequence - 1
            )
            if after and not cursor_reset:
                selected = [
                    entry for entry in entries if entry["sequence"] > after
                ][:limit]
            else:
                selected = entries[-limit:]
            has_more = bool(
                selected and selected[-1]["sequence"] < latest_sequence
            )
            public_entries = [
                {key: value for key, value in entry.items() if key != "size"}
                for entry in selected
            ]
            return {
                "available": True,
                "run_id": self.run_id,
                "run_started_at": self.started_at,
                "entries": public_entries,
                "oldest_sequence": oldest_sequence,
                "latest_sequence": latest_sequence,
                "cursor_reset": cursor_reset,
                "has_more": has_more,
                "buffer": {
                    "lines": len(entries),
                    "bytes": self._total_bytes,
                    "dropped_total": self._dropped_total,
                    "max_lines": MAX_BUFFER_LINES,
                    "max_bytes": MAX_BUFFER_BYTES,
                    "max_line_bytes": MAX_LINE_BYTES,
                },
            }

    def _trim_locked(self):
        while self._entries and (
            len(self._entries) > MAX_BUFFER_LINES
            or self._total_bytes > MAX_BUFFER_BYTES
        ):
            removed = self._entries.popleft()
            self._total_bytes -= removed["size"]
            self._dropped_total += 1


ui_log_buffer = UiLogBuffer()
