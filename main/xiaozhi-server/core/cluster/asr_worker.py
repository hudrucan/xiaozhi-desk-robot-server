"""Turn-pinned ASR admission, ordered input and bounded owned tasks."""
import asyncio
import hmac
import logging
import time
import uuid
from dataclasses import dataclass, field

from . import asr_protocol as wire
from .protocol import valid_reply_subject

LOGGER = logging.getLogger('xiaozhi.worker.asr')


@dataclass
class Turn:
    request: dict
    token: str = field(default_factory=lambda: uuid.uuid4().hex)
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(64))
    seq: int = 0
    lease: float = field(default_factory=time.monotonic)
    ending: bool = False
    finishing: bool = False
    task: object = None


class ASRService:
    def __init__(self, client, worker_id, bundle, pipeline_factory, activity=None):
        self.client, self.worker_id, self.bundle = client, worker_id, bundle
        self.pipeline_factory, self.activity = pipeline_factory, activity
        self.turns, self.stopping = {}, False

    async def start(self):
        await self.client.subscribe(wire.OPEN_SUBJECT, queue=wire.QUEUE_GROUP, cb=self.open,
            pending_msgs_limit=16, pending_bytes_limit=32768)
        await self.client.subscribe(wire.input_subject(self.worker_id), cb=self.input,
            pending_msgs_limit=128, pending_bytes_limit=128 * (wire.MAX_AUDIO + 514))

    async def send(self, subject, turn, kind, **fields):
        if self.client.is_connected:
            await asyncio.wait_for(self.client.publish(subject, wire.event(turn.request['turn_id'],
                self.worker_id, turn.request['revision'], turn.token, kind, **fields)), timeout=1)

    async def fail(self, turn, code):
        if turn.finishing:
            return
        turn.finishing = True
        LOGGER.warning('ASR turn failed; worker_id=%s turn_id=%s code=%s frames=%d',
                       self.worker_id, turn.request['turn_id'], code, turn.seq)
        try:
            await self.send(turn.request['result_subject'], turn, 'error', error=code)
        finally:
            if turn.task and turn.task is not asyncio.current_task():
                turn.task.cancel()

    async def open(self, message):
        if self.stopping or not valid_reply_subject(message.reply) or not message.reply.startswith('_INBOX.'):
            return
        try:
            request = wire.open_request(message.data)
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            return
        turn = Turn(request)
        error = ('asr_revision_mismatch' if request['revision'] != self.bundle['revision'] else
                 'asr_expired' if request['deadline_ms'] <= time.time() * 1000 else
                 'asr_busy' if len(self.turns) >= 2 or request['turn_id'] in self.turns else None)
        if error:
            await self.send(message.reply, turn, 'error', error=error)
            return
        self.turns[request['turn_id']] = turn
        LOGGER.info('ASR turn admitted; worker_id=%s core_id=%s turn_id=%s',
                    self.worker_id, request['core_id'], request['turn_id'])
        turn.task = asyncio.create_task(self.execute(turn))
        def finished(task):
            if self.turns.get(request['turn_id']) is turn:
                self.turns.pop(request['turn_id'], None)
            if not task.cancelled():
                task.exception()
        turn.task.add_done_callback(finished)
        try:
            await self.send(message.reply, turn, 'admitted')
        except Exception:
            turn.task.cancel()

    async def input(self, message):
        try:
            value, audio = wire.unpack(message.data)
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            return
        turn = self.turns.get(value['turn_id'])
        if not turn or not hmac.compare_digest(turn.token, value['token']):
            return
        if not valid_reply_subject(message.reply) or not message.reply.startswith('_INBOX.'):
            return
        turn.lease = time.monotonic()
        kind = value['kind']
        if kind == 'cancel':
            if not turn.finishing:
                turn.finishing = True
                turn.task.cancel()
        elif kind in ('audio', 'end'):
            if value['seq'] != turn.seq:
                await self.fail(turn, 'asr_gap')
                return
            # Auto endpointing may finish while the core is still sending its
            # last silence frames. Acknowledge their order without feeding them
            # into an already-finalizing provider stream.
            if not turn.ending:
                try:
                    turn.queue.put_nowait((kind, audio))
                except asyncio.QueueFull:
                    await self.fail(turn, 'asr_overflow')
                    return
            if kind == 'audio':
                turn.seq += 1
            else:
                turn.ending = True
        try:
            await self.send(message.reply, turn, 'ack', seq=turn.seq)
        except Exception:
            turn.task.cancel()

    async def execute(self, turn):
        pipeline = None
        async def watch_lease():
            while True:
                await asyncio.sleep(0.5)
                if not self.client.is_connected or time.monotonic() - turn.lease > wire.LEASE_SECONDS:
                    try:
                        await self.fail(turn, 'asr_unavailable')
                    except Exception:
                        turn.task.cancel()
                    return
        watchdog = asyncio.create_task(watch_lease())
        if self.activity:
            self.activity.change('asr', 1)
        try:
            async def partial(text):
                await self.send(turn.request['result_subject'], turn, 'partial', text=text)
            pipeline = self.pipeline_factory(self.bundle, turn.request['mode'], partial)
            async with asyncio.timeout(max(0, (turn.request['deadline_ms'] - time.time() * 1000) / 1000)):
                await pipeline.start()
                while True:
                    if not self.client.is_connected or time.monotonic() - turn.lease > wire.LEASE_SECONDS:
                        await self.fail(turn, 'asr_unavailable')
                        return
                    try:
                        kind, data = await asyncio.wait_for(turn.queue.get(), timeout=0.5)
                    except asyncio.TimeoutError:
                        continue
                    ended = kind == 'end' or await pipeline.feed(data)
                    if ended:
                        turn.ending = True
                        text = await asyncio.wait_for(pipeline.finish(), timeout=5)
                        turn.finishing = True
                        LOGGER.info('ASR final ready; worker_id=%s turn_id=%s frames=%d',
                                    self.worker_id, turn.request['turn_id'], turn.seq)
                        await self.send(turn.request['result_subject'], turn, 'final', text=text)
                        return
        except asyncio.CancelledError:
            pass
        except asyncio.TimeoutError:
            await self.fail(turn, 'asr_expired')
        except ValueError:
            await self.fail(turn, 'asr_invalid_audio')
        except Exception:
            LOGGER.warning('ASR execution failed; worker_id=%s', self.worker_id)
            try:
                await self.fail(turn, 'asr_provider_failed')
            except Exception:
                pass
        finally:
            watchdog.cancel()
            await asyncio.gather(watchdog, return_exceptions=True)
            if pipeline:
                try:
                    await asyncio.wait_for(pipeline.close(), timeout=5)
                except Exception:
                    LOGGER.warning('ASR close unavailable; worker_id=%s', self.worker_id)
            self.turns.pop(turn.request['turn_id'], None)
            if self.activity:
                self.activity.change('asr', -1)
            LOGGER.info('ASR turn released; worker_id=%s inflight=%d', self.worker_id, len(self.turns))

    async def stop(self):
        self.stopping = True
        tasks = [turn.task for turn in self.turns.values()]
        for turn in self.turns.values():
            if not turn.finishing:
                turn.finishing = True
                turn.task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
