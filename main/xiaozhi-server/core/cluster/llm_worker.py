"""Opt-in bounded text LLM worker, preserving the original empty ping contract."""
import asyncio
import hashlib
import logging
import time
from collections import OrderedDict

from . import llm_protocol as protocol
from . import llm_stream_protocol as streaming
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
        self.activity = None
        self.streaming_jobs = set()

    async def _start(self):
        await super()._start()
        await self.client.subscribe(protocol.SUBJECT, queue=protocol.QUEUE_GROUP, cb=self._receive,
                                    pending_msgs_limit=16, pending_bytes_limit=16 * protocol.MAX_REQUEST_BYTES)
        await self.client.subscribe(streaming.SUBJECT, queue=streaming.QUEUE_GROUP, cb=self._receive_stream,
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
        await self._admit(message, streamed=False)

    async def _receive_stream(self, message):
        await self._admit(message, streamed=True)

    async def _admit(self, message, *, streamed):
        if self.stopping or not valid_reply_subject(message.reply) or not message.reply.startswith('_INBOX.'):
            return
        try:
            value = streaming.request(message.data) if streamed else protocol.request(message.data)
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
            if streamed:
                # Do not block the admission subscription waiting for an ACK.
                # Errors have no provider job or streaming state to retain.
                try:
                    if self.client.is_connected:
                        await asyncio.wait_for(self.client.publish(message.reply,
                            streaming.event(value, self.config.worker_id, 'error', 0, error=error)), timeout=2)
                except Exception:
                    LOGGER.warning('LLM admission reply unavailable; worker_id=%s', self.config.worker_id)
            else:
                await self._send(message.reply, value, error=error)
            return
        execute = self._execute_stream if streamed else self._execute
        task = asyncio.create_task(execute(message.reply, value))
        if streamed:
            self.streaming_jobs.add(task)
        self.jobs[value['request_id']] = (value['cancel_token'], task)
        if self.activity:
            self.activity.change('llm', 1)
        task.add_done_callback(lambda done: self._finished(value['request_id'], done))
        LOGGER.info('LLM job admitted; worker_id=%s revision=%d inflight=%d',
                    self.config.worker_id, self.bundle['revision'], len(self.jobs))

    def _finished(self, request_id, task):
        self.streaming_jobs.discard(task)
        if self.activity:
            self.activity.change('llm', -1)
        job = self.jobs.get(request_id)
        if job and job[1] is task:
            self.jobs.pop(request_id, None)
        if not task.cancelled():
            task.exception()  # Observe any unexpected cleanup exception.

    async def _disconnected(self):
        # A stream cannot resume safely across Core NATS at-most-once gaps.
        # Subscriptions reconnect normally for subsequent, newly owned turns.
        for task in tuple(self.streaming_jobs):
            task.cancel()
        await super()._disconnected()

    async def _stream_event(self, subject, value, kind, seq, **fields):
        if not self.client.is_connected:
            raise ConnectionError('LLM stream unavailable')
        data = streaming.event(value, self.config.worker_id, kind, seq, **fields)
        try:
            reply = await asyncio.wait_for(self.client.request(subject, data,
                timeout=streaming.ACK_SECONDS), timeout=streaming.ACK_SECONDS + .25)
            streaming.validate_ack(reply.data,
                streaming.parse_event(data, value['request_id'], value['revision']))
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ConnectionError('LLM stream acknowledgment unavailable') from None

    async def _execute_stream(self, reply_subject, value):
        stream, seq = None, 0
        try:
            seconds = max(0, (value['deadline_ms'] - time.time() * 1000) / 1000)
            async with asyncio.timeout(min(seconds, protocol.MAX_SECONDS)):
                await self._stream_event(reply_subject, value, 'started', seq)
                seq += 1
                messages = ([{'role': 'system', 'content': self.bundle['prompt']}]
                    if self.bundle['prompt'] else []) + value['dialogue']
                total, digest = 0, hashlib.sha256()
                stream = self.provider.response_text_async(messages)
                async for part in stream:
                    if not isinstance(part, str):
                        raise ValueError('Invalid provider chunk')
                    encoded = part.encode('utf-8')
                    if total + len(encoded) > protocol.MAX_TEXT_BYTES:
                        await self._stream_event(reply_subject, value, 'error', seq, error='llm_output_too_large')
                        return
                    total += len(encoded)
                    digest.update(encoded)
                    for text in streaming.chunks(part):
                        if seq > streaming.MAX_CHUNKS:
                            await self._stream_event(reply_subject, value, 'error', seq, error='llm_output_too_large')
                            return
                        await self._stream_event(reply_subject, value, 'chunk', seq, text=text)
                        seq += 1
                if not total:
                    await self._stream_event(reply_subject, value, 'error', seq, error='llm_empty_response')
                else:
                    await self._stream_event(reply_subject, value, 'complete', seq,
                        text_bytes=total, sha256=digest.hexdigest())
        except asyncio.CancelledError:
            LOGGER.info('LLM stream cancelled; worker_id=%s', self.config.worker_id)
        except Exception as error:
            # Public errors are fixed codes; provider/transport strings and
            # partial output are never included in logs or terminal failures.
            code = ('llm_expired' if isinstance(error, asyncio.TimeoutError) else
                    'llm_stream_unavailable' if isinstance(error, ConnectionError) else 'llm_provider_failed')
            LOGGER.warning('LLM stream failed; worker_id=%s code=%s', self.config.worker_id, code)
            try:
                await self._stream_event(reply_subject, value, 'error', seq, error=code)
            except Exception:
                pass
        finally:
            try:
                if stream is not None:
                    await asyncio.wait_for(stream.aclose(), timeout=4)
            except Exception:
                LOGGER.warning('LLM stream close unavailable; worker_id=%s', self.config.worker_id)
            finally:
                self.jobs.pop(value['request_id'], None)
                LOGGER.info('LLM stream released; worker_id=%s inflight=%d', self.config.worker_id, len(self.jobs))

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
            LOGGER.info('LLM job expired; worker_id=%s', self.config.worker_id)
            await self._send(reply_subject, value, error='llm_expired')
        except asyncio.CancelledError:
            # Core no longer owns/waits for this result. Do not send late text.
            LOGGER.info('LLM job cancelled; worker_id=%s', self.config.worker_id)
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
                LOGGER.info('LLM job released; worker_id=%s inflight=%d', self.config.worker_id, len(self.jobs))

    async def _shutdown(self):
        self.stopping = True
        jobs = [task for _, task in self.jobs.values()]
        for task in jobs:
            task.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        await super()._shutdown()
