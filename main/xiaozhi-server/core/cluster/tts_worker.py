"""One native synthesis slot, leased to one segment with targeted PCM pulls."""
import asyncio
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass, field

from . import tts_protocol as wire
from .protocol import valid_reply_subject

LOGGER = logging.getLogger('xiaozhi.worker.tts')


@dataclass
class Job:
    value: dict
    lease: float = field(default_factory=time.monotonic)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    task: object = None
    native: object = None
    pcm: bytes = b''
    rate: int = 0
    checksum: str = ''
    elapsed_ms: int = 0
    offset: int = 0
    error: str = ''
    started: bool = False


class TTSService:
    def __init__(self, client, worker_id, bundle, engine, activity=None):
        self.client, self.worker_id, self.bundle, self.engine = client, worker_id, bundle, engine
        self.activity, self.job, self.stopping = activity, None, False

    async def start(self):
        await self.client.subscribe(wire.subject(self.worker_id), cb=self.receive,
            pending_msgs_limit=32, pending_bytes_limit=32 * wire.MAX_REQUEST)

    def available(self):
        return not self.stopping and self.job is None and not (self.activity and self.activity.counts.get('asr', 0))

    async def send(self, subject, header, pcm=b''):
        if self.client.is_connected:
            await asyncio.wait_for(self.client.publish(subject, wire.pack(header, pcm)), 1)

    async def receive(self, message):
        if not valid_reply_subject(message.reply) or not message.reply.startswith('_INBOX.'):
            return
        try:
            value = wire.request(message.data)
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            return
        try:
            if value['op'] == 'status':
                await self.send(message.reply, {'protocol': wire.PROTOCOL, 'worker_id': self.worker_id,
                    'revision': self.bundle['revision'], 'fingerprint': self.bundle['fingerprint'],
                    'state': 'idle' if self.available() else 'busy'})
                return
            error = ('tts_expired' if value['deadline_ms'] <= time.time() * 1000 else
                'tts_revision_mismatch' if value['revision'] != self.bundle['revision'] else
                'tts_voice_mismatch' if value['fingerprint'] != self.bundle['fingerprint'] else None)
            if not error and value['op'] == 'admit':
                if not self.available():
                    error = 'tts_busy'
                else:
                    job = self.job = Job(value)
                    # Admission reserves the only slot before publication yields.
                    job.task = asyncio.create_task(self.execute(job))
                    job.task.add_done_callback(lambda task: self.finished(job, task))
                    await self.send(message.reply, wire.response_header(value, self.worker_id, 'admitted'))
                    return
            job = self.job
            if not error and (job is None or any(value[k] != job.value[k] for k in
                    ('job_id', 'core_id', 'index', 'revision', 'fingerprint'))
                    or not hmac.compare_digest(value['token'], job.value['token'])):
                error = 'tts_unavailable'
            if error:
                await self.send(message.reply, wire.response_header(value, self.worker_id, 'error', error=error))
                return
            job.lease = time.monotonic()
            if value['op'] in ('cancel', 'done'):
                if value['op'] == 'done' and (not job.pcm or job.offset != len(job.pcm)):
                    await self.send(message.reply, wire.response_header(value, self.worker_id, 'error', error='tts_invalid_result'))
                    return
                job.release.set()
                job.task.cancel()
                await self.send(message.reply, wire.response_header(value, self.worker_id, 'released'))
            elif job.error:
                await self.send(message.reply, wire.response_header(value, self.worker_id, 'error', error=job.error))
            elif not job.pcm:
                await self.send(message.reply, wire.response_header(value, self.worker_id, 'pending'))
            elif value['offset'] != job.offset or job.offset >= len(job.pcm):
                await self.send(message.reply, wire.response_header(value, self.worker_id, 'error', error='tts_invalid_result'))
            else:
                offset = job.offset
                pcm = job.pcm[offset:offset + wire.MAX_PCM_CHUNK]
                job.offset += len(pcm)
                await self.send(message.reply, wire.response_header(value, self.worker_id, 'audio',
                    offset=offset, total_bytes=len(job.pcm), sample_rate=job.rate,
                    sha256=job.checksum, synth_ms=job.elapsed_ms), pcm)
        except Exception:
            LOGGER.warning('TTS reply unavailable; worker_id=%s', self.worker_id)

    def finished(self, job, task):
        if not task.cancelled():
            task.exception()
        # Cancellation can arrive after admission but before execute gets its
        # first event-loop turn. There is no native work/activity to join then.
        if not job.started and self.job is job:
            self.job = None

    async def execute(self, job):
        job.started = True
        if self.activity:
            self.activity.change('tts', 1)
        async def watch():
            while True:
                await asyncio.sleep(.25)
                if (not self.client.is_connected or time.monotonic() - job.lease > wire.LEASE_SECONDS
                        or time.time() * 1000 >= job.value['deadline_ms']):
                    job.task.cancel()
                    return
        watchdog = asyncio.create_task(watch())
        try:
            started = time.monotonic()
            for attempt in range(self.bundle['options']['max_retries'] + 1):
                job.native = asyncio.create_task(asyncio.to_thread(self.engine.generate, job.value['text']))
                try:
                    pcm, rate = await asyncio.shield(job.native)
                    break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if attempt == self.bundle['options']['max_retries']:
                        raise
            if (not isinstance(pcm, bytes) or type(rate) is not int or rate not in wire.RATES
                    or not 0 < len(pcm) <= min(wire.MAX_PCM, rate * 2 * 30) or len(pcm) % 2):
                raise ValueError('Invalid native PCM result')
            job.pcm, job.rate = pcm, rate
            job.checksum = hashlib.sha256(pcm).hexdigest()
            job.elapsed_ms = round((time.monotonic() - started) * 1000)
            LOGGER.info('TTS segment synthesized; worker_id=%s index=%d synth_ms=%d audio_ms=%d',
                self.worker_id, job.value['index'], job.elapsed_ms, len(pcm) * 500 // rate)
            await job.release.wait()
        except asyncio.CancelledError:
            pass
        except Exception:
            job.error = 'tts_provider_failed'
            LOGGER.warning('TTS synthesis failed; worker_id=%s', self.worker_id)
            await job.release.wait()
        finally:
            watchdog.cancel()
            try:
                await asyncio.gather(watchdog, return_exceptions=True)
            except asyncio.CancelledError:
                pass
            try:
                # Repeated stop/disconnect signals must not cancel the native
                # thread future or free the slot before synthesis has joined.
                if job.native:
                    while not job.native.done():
                        try:
                            await asyncio.shield(job.native)
                        except asyncio.CancelledError:
                            continue
                        except Exception:
                            break
                    if not job.native.cancelled():
                        job.native.exception()
            finally:
                if self.job is job:
                    self.job = None
                if self.activity:
                    self.activity.change('tts', -1)

    async def stop(self):
        self.stopping = True
        if self.job:
            job = self.job
            job.release.set()
            job.task.cancel()
            await asyncio.gather(job.task, return_exceptions=True)
