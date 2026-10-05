"""Stateless Core NATS ping worker; no provider or device runtime imports."""

import asyncio
import logging

from nats.aio.client import Client
from nats.aio.msg import Msg

from .nats_config import NatsConfig
from .protocol import (
    MAX_REQUEST_BYTES,
    PING_SUBJECT,
    QUEUE_GROUP,
    ping_response,
    targeted_ping_subject,
    valid_reply_subject,
)

LOGGER = logging.getLogger("xiaozhi.worker")


class Worker:
    def __init__(self, config: NatsConfig, stop: asyncio.Event):
        self.config = config
        self.stop = stop
        self.client = Client()
        self.response = ping_response(config.worker_id)

    async def _disconnected(self):
        LOGGER.warning("NATS disconnected; worker_id=%s", self.config.worker_id)

    async def _reconnected(self):
        LOGGER.info("NATS reconnected; worker_id=%s", self.config.worker_id)

    async def _closed(self):
        LOGGER.info("NATS closed; worker_id=%s", self.config.worker_id)
        self.stop.set()

    async def _error(self, error: Exception):
        # Exception messages and connection URLs may contain credentials.
        LOGGER.warning("NATS operation failed; worker_id=%s", self.config.worker_id)

    async def _ping(self, message: Msg):
        if (
            not valid_reply_subject(message.reply)
            or len(message.data) > MAX_REQUEST_BYTES
        ):
            return
        # Ignore payload contents, including invalid UTF-8/JSON. Do not echo
        # request headers: Msg.respond() would forward them to the reply.
        try:
            await asyncio.wait_for(
                self.client.publish(message.reply, self.response), timeout=2
            )
        except Exception:
            LOGGER.warning("Ping reply failed; worker_id=%s", self.config.worker_id)

    async def _start(self):
        await self.client.connect(
            servers=list(self.config.servers),
            user=self.config.username,
            password=self.config.password,
            name=f"xiaozhi-worker-{self.config.worker_id}",
            allow_reconnect=True,
            max_reconnect_attempts=-1,
            reconnect_time_wait=2,
            connect_timeout=5,
            ping_interval=20,
            max_outstanding_pings=2,
            drain_timeout=10,
            pending_size=256 * 1024,
            flush_timeout=2,
            error_cb=self._error,
            disconnected_cb=self._disconnected,
            reconnected_cb=self._reconnected,
            closed_cb=self._closed,
        )
        LOGGER.info("NATS connected; worker_id=%s", self.config.worker_id)
        for subject, queue in (
            (PING_SUBJECT, QUEUE_GROUP),
            (targeted_ping_subject(self.config.worker_id), ""),
        ):
            await self.client.subscribe(
                subject,
                queue=queue,
                cb=self._ping,
                pending_msgs_limit=64,
                pending_bytes_limit=64 * MAX_REQUEST_BYTES,
            )
        # The client replays subscriptions on reconnect. A startup flush timeout
        # must not turn a transient node failure into worker termination.
        LOGGER.info("Worker subscriptions registered; worker_id=%s", self.config.worker_id)

    async def _shutdown(self):
        if self.client.is_closed:
            return
        LOGGER.info("Worker draining; worker_id=%s", self.config.worker_id)
        try:
            # Client drain processes queued requests in both subscriptions,
            # flushes replies, then closes the connection.
            await asyncio.wait_for(self.client.drain(), timeout=25)
        except Exception:
            LOGGER.warning(
                "NATS drain unavailable or timed out; worker_id=%s",
                self.config.worker_id,
            )
        finally:
            if not self.client.is_closed:
                await asyncio.wait_for(self.client.close(), timeout=5)

    async def run(self):
        LOGGER.info("Worker starting; worker_id=%s", self.config.worker_id)
        startup = asyncio.create_task(self._start())
        stopped = asyncio.create_task(self.stop.wait())
        try:
            done, _ = await asyncio.wait(
                (startup, stopped), return_when=asyncio.FIRST_COMPLETED
            )
            if stopped not in done:
                await startup
                await stopped
        finally:
            # A signal also cancels initial connection/retry, not just ready state.
            for task in (startup, stopped):
                if not task.done():
                    task.cancel()
            await asyncio.gather(startup, stopped, return_exceptions=True)
            await self._shutdown()
