"""Bounded Core NATS requests, owned by the standalone core lifecycle."""

import asyncio
import json
import logging
import time
import uuid

from .nats_config import NatsConnectionConfig, validate_worker_id
from .protocol import MAX_REPLY_BYTES, PING_SUBJECT, targeted_ping_subject

LOGGER = logging.getLogger("xiaozhi.core.rpc")
MAX_INFLIGHT = 8
REQUEST_TIMEOUT = 2


class WorkerRpcError(Exception):
    """Public, fixed error code; transport exceptions never reach clients."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def parse_ping_reply(data, expected_worker=None):
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_REPLY_BYTES:
        raise ValueError("Invalid worker reply")
    def unique(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("Invalid worker reply")
        return result
    value = json.loads(data.decode("utf-8"), object_pairs_hook=unique)
    if (not isinstance(value, dict) or set(value) != {"protocol", "worker_id", "status", "capabilities"}
            or value["protocol"] != "xiaozhi-worker-v1" or value["status"] != "ok"
            or value["capabilities"] != [] or not isinstance(value["worker_id"], str)):
        raise ValueError("Invalid worker reply")
    validate_worker_id(value["worker_id"])
    if expected_worker is not None and value["worker_id"] != expected_worker:
        raise ValueError("Unexpected worker identity")
    return value


def _client():
    from nats.aio.client import Client
    return Client()


class WorkerRPC:
    def __init__(self, config: NatsConnectionConfig, core_id, *, client_factory=_client):
        self.config, self.core_id = config, core_id
        self.client = client_factory()
        self.state = "not_started"
        self.stopping = False
        self.calls = set()
        self.streams = set()
        self.startup = None
        self.completed = 0
        self.failed = 0

    def status(self):
        return {"protocol": "xiaozhi-core-worker-rpc-v1", "state": self.state,
                "inflight": len(self.calls), "completed": self.completed, "failed": self.failed}

    async def _disconnected(self):
        for stream in tuple(self.streams):
            stream.fail()
        if not self.stopping:
            self.state = "disconnected"
        LOGGER.warning("NATS disconnected; core_id=%s", self.core_id)

    async def _reconnected(self):
        if not self.stopping:
            self.state = "connected"
        LOGGER.info("NATS reconnected; core_id=%s", self.core_id)

    async def _closed(self):
        for stream in tuple(self.streams):
            stream.fail()
        self.state = "closed"
        LOGGER.info("NATS closed; core_id=%s", self.core_id)

    async def _error(self, error):
        LOGGER.warning("NATS operation failed; core_id=%s", self.core_id)

    async def _connect(self):
        while not self.stopping:
            try:
                await self._connect_once()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                self.state = "unavailable"
                LOGGER.warning("NATS startup unavailable; core_id=%s", self.core_id)
                await asyncio.sleep(2)

    async def _connect_once(self):
        await self.client.connect(
            servers=list(self.config.servers), user=self.config.username,
            password=self.config.password, name=f"xiaozhi-core-{self.core_id}",
            allow_reconnect=True, max_reconnect_attempts=-1, reconnect_time_wait=2,
            connect_timeout=5, ping_interval=20, max_outstanding_pings=2,
            drain_timeout=3, pending_size=256 * 1024, flush_timeout=2,
            disconnected_cb=self._disconnected, reconnected_cb=self._reconnected,
            closed_cb=self._closed, error_cb=self._error,
        )
        if not self.stopping:
            self.state = "connected"
            LOGGER.info("NATS connected; core_id=%s", self.core_id)

    def start(self):
        if self.startup is not None or self.stopping:
            raise RuntimeError("Worker RPC already started or stopping")
        self.state = "connecting"
        self.startup = asyncio.create_task(self._connect())

    async def ping(self, worker_id=None):
        # Validate before constructing any subject. No request-controlled reply
        # subjects, headers, config or credentials are sent to the worker.
        if worker_id is not None:
            validate_worker_id(worker_id)
        if self.stopping or not self.client.is_connected:
            raise WorkerRpcError("worker_rpc_unavailable")
        if len(self.calls) >= MAX_INFLIGHT:
            raise WorkerRpcError("worker_rpc_busy")
        task = asyncio.current_task()
        self.calls.add(task)
        try:
            subject = PING_SUBJECT if worker_id is None else targeted_ping_subject(worker_id)
            response = await asyncio.wait_for(self.client.request(subject, b"", timeout=REQUEST_TIMEOUT),
                                              timeout=REQUEST_TIMEOUT + 0.25)
            try:
                result = parse_ping_reply(response.data, worker_id)
            except (ValueError, TypeError, RecursionError, UnicodeError):
                raise WorkerRpcError("worker_rpc_invalid_reply") from None
            if self.stopping:
                raise WorkerRpcError("worker_rpc_unavailable")
            self.completed += 1
            return result
        except asyncio.CancelledError:
            raise
        except WorkerRpcError:
            self.failed += 1
            raise
        except Exception:
            self.failed += 1
            LOGGER.warning("Worker request failed; core_id=%s", self.core_id)
            raise WorkerRpcError("worker_rpc_failed") from None
        finally:
            self.calls.discard(task)

    async def generate(self, revision, messages, seconds=30):
        from . import llm_protocol as protocol
        if type(revision) is not int or revision < 1 or type(seconds) is not int or not 1 <= seconds <= protocol.MAX_SECONDS:
            raise ValueError("Invalid LLM revision or deadline")
        protocol.dialogue(messages)
        if self.stopping or not self.client.is_connected:
            raise WorkerRpcError("worker_rpc_unavailable")
        if len(self.calls) >= MAX_INFLIGHT:
            raise WorkerRpcError("worker_rpc_busy")
        request_id, token = uuid.uuid4().hex, uuid.uuid4().hex
        payload = protocol.encode({"protocol": protocol.PROTOCOL, "request_id": request_id,
            "cancel_token": token, "core_id": self.core_id, "revision": revision,
            "deadline_ms": int(time.time() * 1000) + seconds * 1000, "dialogue": messages}, protocol.MAX_REQUEST_BYTES)
        cancellation = protocol.encode({"protocol": protocol.PROTOCOL,
            "request_id": request_id, "cancel_token": token}, 512)
        task = asyncio.current_task()
        self.calls.add(task)
        try:
            result = await asyncio.wait_for(self.client.request(protocol.SUBJECT, payload, timeout=seconds),
                                            timeout=seconds + 0.25)
            try:
                value = protocol.reply(result.data, request_id, revision)
            except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
                raise WorkerRpcError("worker_rpc_invalid_reply") from None
            if self.stopping:
                raise WorkerRpcError("worker_rpc_unavailable")
            self.completed += 1
            return value
        except asyncio.CancelledError:
            raise
        except WorkerRpcError:
            self.failed += 1
            raise
        except Exception:
            self.failed += 1
            raise WorkerRpcError("worker_rpc_failed") from None
        finally:
            # Best-effort broadcast, never buffered through reconnect. The token
            # prevents an unrelated core request from cancelling this job.
            try:
                if self.client.is_connected:
                    await asyncio.wait_for(self.client.publish(protocol.CANCEL_SUBJECT, cancellation), timeout=1)
            except Exception:
                LOGGER.warning("LLM cancellation unavailable; core_id=%s", self.core_id)
            finally:
                self.calls.discard(task)

    async def generate_stream(self, revision, messages, on_chunk, seconds=30):
        from . import llm_protocol as protocol
        from .llm_stream_client import LLMStreamClient
        if (type(revision) is not int or revision < 1 or type(seconds) is not int
                or not 1 <= seconds <= protocol.MAX_SECONDS or not callable(on_chunk)):
            raise ValueError('Invalid LLM stream parameters')
        protocol.dialogue(messages)
        if self.stopping or not self.client.is_connected:
            raise WorkerRpcError('worker_rpc_unavailable')
        if len(self.calls) >= MAX_INFLIGHT:
            raise WorkerRpcError('worker_rpc_busy')
        stream = LLMStreamClient(self.client, self.core_id, revision, messages, seconds, on_chunk)
        task = asyncio.current_task()
        self.calls.add(task)
        self.streams.add(stream)
        try:
            result = await stream.run()
            if self.stopping:
                raise WorkerRpcError('worker_rpc_unavailable')
            self.completed += 1
            return result
        except asyncio.CancelledError:
            raise
        except WorkerRpcError:
            self.failed += 1
            raise
        finally:
            self.streams.discard(stream)
            self.calls.discard(task)

    async def close(self):
        if self.stopping:
            return
        self.stopping = True
        self.state = "stopping"
        for stream in tuple(self.streams):
            stream.fail()
        pending = list(self.calls)
        if self.startup is not None:
            pending.append(self.startup)
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        try:
            if not self.client.is_closed:
                await asyncio.wait_for(self.client.drain(), timeout=4)
        except Exception:
            LOGGER.warning("NATS drain unavailable; core_id=%s", self.core_id)
        finally:
            try:
                if not self.client.is_closed:
                    await asyncio.wait_for(self.client.close(), timeout=2)
            except Exception:
                LOGGER.warning("NATS close unavailable; core_id=%s", self.core_id)
            finally:
                self.state = "closed"
