"""Core-owned, bounded ASR stream over an existing NATS connection."""
import asyncio
import logging
import time
import uuid

from . import asr_protocol as wire
from .llm_protocol import encode
from .worker_rpc import WorkerRpcError

LOGGER = logging.getLogger('xiaozhi.core.asr')


class ASRClient:
    def __init__(self, client, core_id, revision, audio, mode, partial, on_admitted=None):
        self.client, self.core_id, self.revision = client, core_id, revision
        self.audio, self.mode, self.partial = audio, mode, partial
        self.on_admitted = on_admitted
        self.turn_id, self.inbox = uuid.uuid4().hex, '_INBOX.' + uuid.uuid4().hex
        self.worker_id = self.token = self.subscription = None
        self.final = asyncio.get_running_loop().create_future()
        self.seq, self.tasks = 0, []
        self.remote_terminal = False
        self.stopping = False
        self.deadline = time.monotonic() + wire.MAX_SECONDS

    async def receive(self, message):
        try:
            value = wire.result(message.data, self.turn_id, self.revision, self.worker_id, self.token)
            # Nothing from a result inbox is authoritative before admission.
            if self.worker_id is None or value['kind'] not in ('partial', 'final', 'error'):
                return
            if value['kind'] == 'partial' and not self.final.done():
                await asyncio.wait_for(self.partial(value['text']), timeout=1)
            elif value['kind'] == 'final' and not self.final.done():
                self.remote_terminal = True
                self.final.set_result(value['text'])
            elif value['kind'] == 'error' and not self.final.done():
                self.remote_terminal = True
                self.final.set_exception(WorkerRpcError(value['error']))
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            return
        except Exception:
            if not self.final.done():
                self.final.set_exception(WorkerRpcError('asr_unavailable'))

    async def open(self):
        if not self.client.is_connected:
            raise WorkerRpcError('asr_unavailable')
        self.subscription = await self.client.subscribe(self.inbox, cb=self.receive,
            pending_msgs_limit=16, pending_bytes_limit=16 * (wire.MAX_TEXT + 1024))
        await asyncio.wait_for(self.client.flush(), timeout=2)
        response = await asyncio.wait_for(self.client.request(wire.OPEN_SUBJECT, encode({
            'protocol': wire.PROTOCOL, 'turn_id': self.turn_id, 'core_id': self.core_id,
            'revision': self.revision, 'deadline_ms': int(time.time() * 1000) + wire.MAX_SECONDS * 1000,
            'result_subject': self.inbox, 'mode': self.mode, 'audio': self.audio}, 2048), timeout=3), timeout=3.25)
        value = wire.result(response.data, self.turn_id, self.revision)
        if value['kind'] == 'error':
            raise WorkerRpcError(value['error'])
        if value['kind'] != 'admitted':
            raise ValueError('Invalid ASR admission reply')
        self.worker_id, self.token = value['worker_id'], value['token']
        if self.on_admitted is not None:
            self.on_admitted(self.worker_id)
        LOGGER.info('ASR admitted; core_id=%s worker_id=%s turn_id=%s revision=%d',
                    self.core_id, self.worker_id, self.turn_id, self.revision)

    async def input(self, kind, audio=b''):
        if not self.client.is_connected:
            raise WorkerRpcError('asr_unavailable')
        sequence = self.seq
        response = await asyncio.wait_for(self.client.request(wire.input_subject(self.worker_id),
            wire.packet(self.turn_id, self.token, kind, sequence, audio), timeout=2), timeout=2.25)
        value = wire.result(response.data, self.turn_id, self.revision, self.worker_id, self.token)
        expected = sequence + (kind == 'audio')
        if value['kind'] != 'ack' or (kind in ('audio', 'end') and value['seq'] != expected):
            raise WorkerRpcError('asr_gap')
        if kind == 'audio':
            self.seq = expected

    async def lease(self):
        while not self.stopping and not self.final.done():
            await asyncio.sleep(1)
            if self.stopping or self.final.done():
                return
            await self.input('lease')

    async def send_audio(self, queue):
        while not self.stopping and not self.final.done():
            kind, audio = await queue.get()
            if self.stopping or self.final.done():
                return
            await self.input(kind, audio)
            if kind == 'end':
                return

    async def transcribe(self, queue):
        try:
            async with asyncio.timeout(wire.MAX_SECONDS + 1):
                await self.open()
                sender, lease = asyncio.create_task(self.send_audio(queue)), asyncio.create_task(self.lease())
                self.tasks = [sender, lease]
                while True:
                    done, _ = await asyncio.wait([self.final, *self.tasks], return_when=asyncio.FIRST_COMPLETED)
                    if self.final in done:
                        LOGGER.info('ASR terminal received; core_id=%s worker_id=%s turn_id=%s frames=%d',
                                    self.core_id, self.worker_id, self.turn_id, self.seq)
                        return self.final.result()
                    for task in done:
                        task.result()
                        self.tasks.remove(task)
        except asyncio.CancelledError:
            raise
        except WorkerRpcError:
            raise
        except Exception:
            raise WorkerRpcError('asr_unavailable') from None
        finally:
            # An ACK can finish concurrently with cancellation of wait_for.
            # Explicit ownership prevents the sender from entering another
            # queue wait even if that boundary returns normally.
            self.stopping = True
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            # Final/error is terminal: the worker owns provider cleanup, which
            # may still be running. Once it removes the turn, cancel has no ACK;
            # waiting for that ACK adds a request timeout before LLM handoff.
            if self.token and not self.remote_terminal and self.client.is_connected:
                try:
                    await self.input('cancel')
                except Exception:
                    pass
            try:
                if self.subscription:
                    await self.subscription.unsubscribe()
            finally:
                if self.final.done() and not self.final.cancelled():
                    self.final.exception()
                else:
                    self.final.cancel()
