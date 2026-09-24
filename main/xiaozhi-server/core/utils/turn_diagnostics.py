import asyncio
import json
import threading
import time
import uuid

from core.utils.runtime_diagnostics import runtime_diagnostics


INPUT_PREVIEW_LIMIT = 600
OUTPUT_PREVIEW_LIMIT = 1200
TOOL_TEXT_PREVIEW_LIMIT = 800


def _bounded_text(value, limit):
    if value is None:
        return "", False
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)
    text = text.strip()
    return text[:limit], len(text) > limit


def _action_name(result):
    action = getattr(result, "action", None)
    name = getattr(action, "name", None)
    return name.lower() if name else None


class TurnDiagnosticsMixin:
    """Collect bounded, in-memory diagnostics without owning turn behavior."""

    def initialize_turn_diagnostics(self):
        self._turn_metrics_lock = threading.Lock()
        self._turn_metrics = None

    def start_turn_metrics(self, source):
        if not self.config.get("enable_turn_metrics", True):
            return
        now = time.monotonic()
        with self._turn_metrics_lock:
            self._turn_metrics = {
                "turn_id": uuid.uuid4().hex,
                "session_id": self.session_id,
                "device_id": self.device_id,
                "source": source,
                "started_at": now,
                "sentence_id": None,
                "marks": {},
                "tools": {},
                "queue_peaks": {},
            }
            turn_id = self._turn_metrics["turn_id"]
        runtime_diagnostics.begin_turn(self.session_id, turn_id, source)

    def has_active_turn_metrics(self):
        with self._turn_metrics_lock:
            return self._turn_metrics is not None

    def record_turn_input(self, value):
        preview, truncated = _bounded_text(value, INPUT_PREVIEW_LIMIT)
        with self._turn_metrics_lock:
            if self._turn_metrics is None:
                return
            self._turn_metrics["input"] = preview
            self._turn_metrics["input_truncated"] = truncated

    def append_turn_output(self, value):
        preview, truncated = _bounded_text(value, OUTPUT_PREVIEW_LIMIT)
        if not preview:
            return
        with self._turn_metrics_lock:
            if self._turn_metrics is None:
                return
            current = self._turn_metrics.get("output", "")
            separator = "\n" if current else ""
            combined = current + separator + preview
            self._turn_metrics["output"] = combined[:OUTPUT_PREVIEW_LIMIT]
            self._turn_metrics["output_truncated"] = (
                self._turn_metrics.get("output_truncated", False)
                or truncated
                or len(combined) > OUTPUT_PREVIEW_LIMIT
            )

    def mark_turn_metric(self, event, sentence_id=None):
        now = time.monotonic()
        with self._turn_metrics_lock:
            if self._turn_metrics is None:
                return
            if sentence_id is not None:
                self._turn_metrics["sentence_id"] = sentence_id
            self._turn_metrics["marks"].setdefault(event, now)

    def record_queue_depth(self, queue_name, depth):
        with self._turn_metrics_lock:
            if self._turn_metrics is None:
                return
            peaks = self._turn_metrics["queue_peaks"]
            peaks[queue_name] = max(depth, peaks.get(queue_name, 0))

    def enqueue_asr_audio(self, pcm_frame):
        try:
            self.asr_audio_queue.put_nowait(pcm_frame)
        except asyncio.QueueFull as error:
            raise RuntimeError(
                "ASR audio queue overflow; closing the stale connection"
            ) from error
        self.record_queue_depth("asr_audio", self.asr_audio_queue.qsize())

    def start_tool_metric(
        self,
        tool_call_id,
        tool_name,
        arguments=None,
        tool_type=None,
    ):
        now = time.monotonic()
        arguments_preview, arguments_truncated = _bounded_text(
            arguments, TOOL_TEXT_PREVIEW_LIMIT
        )
        with self._turn_metrics_lock:
            if self._turn_metrics is None:
                return
            turn_started_at = self._turn_metrics["started_at"]
            self._turn_metrics["tools"][tool_call_id] = {
                "call_id": str(tool_call_id or ""),
                "name": tool_name,
                "type": tool_type or "unknown",
                "arguments": arguments_preview,
                "arguments_truncated": arguments_truncated,
                "started_at": now,
                "started_ms": round((now - turn_started_at) * 1000, 1),
            }

    def mark_device_mcp_request(self, tool_call_id, request_id):
        now = time.monotonic()
        with self._turn_metrics_lock:
            if self._turn_metrics is None:
                return
            metric = self._turn_metrics["tools"].get(tool_call_id)
            if metric is None:
                return
            metric["mcp_request_id"] = request_id
            metric["request_sent_ms"] = round(
                (now - metric["started_at"]) * 1000, 1
            )

    def mark_device_mcp_response(self, request_id, outcome="received"):
        now = time.monotonic()
        with self._turn_metrics_lock:
            metric = self._find_device_tool_metric(request_id)
            if metric is None:
                return
            metric["response_received_ms"] = round(
                (now - metric["started_at"]) * 1000, 1
            )
            metric["mcp_response_outcome"] = outcome

    def _find_device_tool_metric(self, request_id):
        if self._turn_metrics is None:
            return None
        for metric in self._turn_metrics["tools"].values():
            if metric.get("type") != "device_mcp" or "duration_ms" in metric:
                continue
            if metric.get("mcp_request_id") == request_id:
                return metric
        return None

    def finish_tool_metric(self, tool_call_id, outcome, result=None):
        now = time.monotonic()
        with self._turn_metrics_lock:
            if self._turn_metrics is None:
                return
            metric = self._turn_metrics["tools"].get(tool_call_id)
            if metric is None or "duration_ms" in metric:
                return
            metric["duration_ms"] = round(
                (now - metric.pop("started_at")) * 1000, 1
            )
            metric["outcome"] = outcome
            action = _action_name(result)
            if action:
                metric["action"] = action
            result_preview, result_truncated = _bounded_text(
                getattr(result, "result", result), TOOL_TEXT_PREVIEW_LIMIT
            )
            response_preview, response_truncated = _bounded_text(
                getattr(result, "response", None), TOOL_TEXT_PREVIEW_LIMIT
            )
            if result_preview:
                metric["result"] = result_preview
                metric["result_truncated"] = result_truncated
            if response_preview and response_preview != result_preview:
                metric["response"] = response_preview
                metric["response_truncated"] = response_truncated

    async def observe_tool_call(self, tool_call_id, awaitable):
        """Finalize one tool metric when that tool actually completes."""
        try:
            result = await awaitable
        except Exception as error:
            self.finish_tool_metric(
                tool_call_id,
                "failed",
                result=str(error),
            )
            raise
        action = _action_name(result)
        outcome = "failed" if action in ("error", "notfound") else "completed"
        self.finish_tool_metric(tool_call_id, outcome, result=result)
        return result

    def complete_turn_metrics(self, outcome):
        completed_at = time.monotonic()
        with self._turn_metrics_lock:
            metrics = self._turn_metrics
            self._turn_metrics = None
        if metrics is None:
            return

        started_at = metrics.pop("started_at")
        marks = metrics.pop("marks")
        tools = metrics.pop("tools")
        metrics["outcome"] = outcome
        metrics["total_ms"] = round((completed_at - started_at) * 1000, 1)
        metrics["marks_ms"] = {
            name: round((marked_at - started_at) * 1000, 1)
            for name, marked_at in marks.items()
        }
        for metric in tools.values():
            if "started_at" in metric:
                metric["duration_ms"] = round(
                    (completed_at - metric.pop("started_at")) * 1000, 1
                )
                metric["outcome"] = "incomplete"
        metrics["tools"] = list(tools.values())
        runtime_diagnostics.complete_turn(metrics)
        log_metrics = {
            key: metrics.get(key)
            for key in (
                "turn_id",
                "source",
                "sentence_id",
                "queue_peaks",
                "outcome",
                "total_ms",
                "marks_ms",
            )
        }
        log_metrics["tools"] = [
            {
                key: metric.get(key)
                for key in (
                    "name",
                    "type",
                    "duration_ms",
                    "outcome",
                    "action",
                    "request_sent_ms",
                    "response_received_ms",
                )
                if metric.get(key) is not None
            }
            for metric in metrics["tools"]
        ]
        self.logger.bind(tag=__name__).debug(
            f"Turn metrics: {json.dumps(log_metrics, ensure_ascii=False)}"
        )
