"""Opt-in bounded text LLM worker, preserving the original empty ping contract."""
import asyncio
import logging
import time
from collections import OrderedDict

from . import llm_protocol as protocol
from .protocol import valid_reply_subject
from .worker import Worker

LOGGER = logging.getLogger('xiaozhi.worker.llm')
MAX_JOBS = 2
MAX_TOMBSTONES = 128


class LLMWorker(Worker):
    def __init__(self, config, stop, bundle, provider):
        super().__init__(config, stop)
        self.bundle, self.provider = bundle, provider
        self.jobs = {}
        self.cancelled = OrderedDict()
        self.stopping = False

    async def _start(self):
        await super()._start()
        await self.client.subscribe(protocol.SUBJECT, queue=protocol.QUEUE_GROUP, cb=self._receive,
                                    pending_msgs_limit=16, pending_bytes_limit=16 * protocol.MAX_REQUEST_BYTES)
        # All workers see cancellation, including if it races queue delivery.
        await self.client.subscribe(protocol.CANCEL_SUBJECT, cb=self._cancel,
                                    pending_msgs_limit=128, pending_bytes_limit=128 * 512)
        LOGGER.info('Text LLM registered; worker_id=%s revision=%d', self.config.worker_id, self.bundle['revision'])

    def _prune_cancelled(self):
        now = time.monotonic()
        for key, expires in list(self.cancelled.items()):
            if expires <= now:
                self.cancelled.pop(key, None)

    async def _cancel(self, message):
        try:
            value = protocol.cancellation(message.data)
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            return
        self._prune_cancelled()
        key = (value['request_id'], value['cancel_token'])
        self.cancelled[key] = time.monotonic() + protocol.MAX_SECONDS + 2
        self.cancelled.move_to_end(key)
        while len(self.cancelled) > MAX_TOMBSTONES:
            self.cancelled.popitem(last=False)
        job = self.jobs.get(value['request_id'])
        if job and job[0] == value['cancel_token']:
            job[1].cancel()

    async def _send(self, reply_subject, value, *, text=None, error=None):
        if not self.client.is_connected:
            return  # Never buffer stale results through a reconnect.
        try:
            data = protocol.response(value, self.config.worker_id, text=text, error=error)
            await asyncio.wait_for(self.client.publish(reply_subject, data), timeout=2)
        except Exception:
            LOGGER.warning('LLM reply unavailable; worker_id=%s', self.config.worker_id)

    async def _receive(self, message):
        if self.stopping or not valid_reply_subject(message.reply) or not message.reply.startswith('_INBOX.'):
            return
        try:
            value = protocol.request(message.data)
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            return
        self._prune_cancelled()
        if value['request_id'] in self.jobs:
            return  # Never execute a duplicate request concurrently.
        key = (value['request_id'], value['cancel_token'])
        error = None
        if key in self.cancelled:
            error = 'llm_cancelled'
        elif value['deadline_ms'] <= int(time.time() * 1000):
            error = 'llm_expired'
        elif value['revision'] != self.bundle['revision']:
            error = 'llm_revision_mismatch'
        elif len(self.jobs) >= MAX_JOBS:
            error = 'llm_busy'
        if error:
            await self._send(message.reply, value, error=error)
            return
        task = asyncio.create_task(self._execute(message.reply, value))
        self.jobs[value['request_id']] = (value['cancel_token'], task)
        task.add_done_callback(lambda done: self._finished(value['request_id'], done))

    def _finished(self, request_id, task):
        job = self.jobs.get(request_id)
        if job and job[1] is task:
            self.jobs.pop(request_id, None)
        if not task.cancelled():
            task.exception()  # Observe any unexpected cleanup exception.

    async def _execute(self, reply_subject, value):
        stream = None
        try:
            seconds = max(0, (value['deadline_ms'] - time.time() * 1000) / 1000)
            messages = ([{'role': 'system', 'content': self.bundle['prompt']}] if self.bundle['prompt'] else []) + value['dialogue']
            parts, total = [], 0
            async with asyncio.timeout(min(seconds, protocol.MAX_SECONDS)):
                stream = self.provider.response_text_async(messages)
                async for part in stream:
                    if not isinstance(part, str):
                        raise ValueError('Invalid provider chunk')
                    total += len(part.encode('utf-8'))
                    if total > protocol.MAX_TEXT_BYTES:
                        await self._send(reply_subject, value, error='llm_output_too_large')
                        return
                    if part:
                        parts.append(part)
            if parts:
                await self._send(reply_subject, value, text=''.join(parts))
            else:
                await self._send(reply_subject, value, error='llm_empty_response')
        except asyncio.TimeoutError:
            await self._send(reply_subject, value, error='llm_expired')
        except asyncio.CancelledError:
            # Core no longer owns/waits for this result. Do not send late text.
            pass
        except Exception:
            LOGGER.warning('LLM execution failed; worker_id=%s', self.config.worker_id)
            await self._send(reply_subject, value, error='llm_provider_failed')
        finally:
            try:
                if stream is not None:
                    await asyncio.wait_for(stream.aclose(), timeout=4)
            except Exception:
                LOGGER.warning('LLM stream close unavailable; worker_id=%s', self.config.worker_id)
            finally:
                self.jobs.pop(value['request_id'], None)

    async def _shutdown(self):
        self.stopping = True
        jobs = [task for _, task in self.jobs.values()]
        for task in jobs:
            task.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        await super()._shutdown()
