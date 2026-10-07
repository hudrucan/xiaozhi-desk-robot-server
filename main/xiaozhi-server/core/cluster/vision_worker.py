"""One selected worker owns a bounded image upload and one cancellable inference."""
import asyncio
import hashlib
import hmac
import logging
import time

from . import vision_protocol as wire
from .protocol import valid_reply_subject

LOGGER = logging.getLogger('xiaozhi.worker.vision')


class VisionService:
    def __init__(self, client, worker_id, revision, provider, activity=None):
        self.client, self.worker_id, self.revision = client, worker_id, revision
        self.provider, self.activity = provider, activity
        self.jobs = {}
        self.stopping = False
        self.watchdog = None

    def release(self, key, job):
        if self.jobs.get(key) is job:
            self.jobs.pop(key)
            job['image'].clear()
            if self.activity:
                self.activity.change('llm', -1)

    async def start(self):
        await self.client.subscribe(wire.SUBJECT, queue=wire.QUEUE, cb=self.receive,
                                    pending_msgs_limit=4, pending_bytes_limit=16384)
        await self.client.subscribe(wire.target(self.worker_id), cb=self.receive,
                                    pending_msgs_limit=8, pending_bytes_limit=8 * (wire.CHUNK + 80))
        self.watchdog = asyncio.create_task(self.expire())

    async def expire(self):
        while True:
            await asyncio.sleep(.25)
            for key, job in list(self.jobs.items()):
                if job['value']['deadline_ms'] <= time.time() * 1000 or not self.client.is_connected:
                    if job['task']:
                        job['task'].cancel()
                    else:
                        self.release(key, job)

    async def send(self, reply, job_id, status, **fields):
        await asyncio.wait_for(self.client.publish(reply, wire.encode({
            'protocol':wire.PROTOCOL, 'job_id':job_id, 'worker_id':self.worker_id,
            'status':status, **fields}, wire.MAX_TEXT + 1024)), 1)

    async def receive(self, msg):
        if not valid_reply_subject(msg.reply) or not msg.reply.startswith('_INBOX.'):
            return
        try:
            if msg.data.startswith(b'{'):
                value = wire.request(msg.data)
                key, token = value['job_id'], value['token']
                if value['op'] == 'admit':
                    now = time.time() * 1000
                    error = ('vision_unavailable' if self.stopping or not self.client.is_connected else
                             'vision_revision_mismatch' if value['revision'] != self.revision else
                             'vision_expired' if not now < value['deadline_ms'] <= now + wire.SECONDS * 1000 else
                             'vision_busy' if self.jobs or (self.activity and self.activity.counts['llm'] >= 2) else None)
                    if error:
                        await self.send(msg.reply, key, 'error', error=error)
                        return
                    self.jobs[key] = {'value':value, 'image':bytearray(), 'task':None}
                    if self.activity:
                        self.activity.change('llm', 1)
                    try:
                        await self.send(msg.reply, key, 'admitted')
                    except Exception:
                        self.release(key, self.jobs[key])
                        raise
                    return
                payload, offset = None, None
            else:
                if not 68 < len(msg.data) <= wire.CHUNK + 68:
                    return
                key = wire.identity(msg.data[:32].decode('ascii'))
                token = wire.identity(msg.data[32:64].decode('ascii'))
                offset = int.from_bytes(msg.data[64:68], 'big')
                payload = msg.data[68:]
                value = {'op':'chunk'}
            job = self.jobs.get(key)
            if job is None or not hmac.compare_digest(token, job['value']['token']):
                await self.send(msg.reply, key, 'error', error='vision_unavailable')
                return
            if value['op'] == 'cancel':
                if job['task']:
                    job['task'].cancel()
                else:
                    self.release(key, job)
                await self.send(msg.reply, key, 'error', error='vision_unavailable')
                return
            if self.stopping or job['value']['deadline_ms'] <= time.time() * 1000 or job['task'] is not None:
                await self.send(msg.reply, key, 'error', error='vision_unavailable')
                return
            if value['op'] == 'chunk':
                if offset != len(job['image']) or offset + len(payload) > job['value']['size']:
                    self.release(key, job)
                    await self.send(msg.reply, key, 'error', error='vision_invalid_request')
                    return
                job['image'].extend(payload)
                await self.send(msg.reply, key, 'chunk', offset=len(job['image']))
            elif (len(job['image']) != job['value']['size'] or not wire.jpeg(job['image'])
                  or hashlib.sha256(job['image']).hexdigest() != job['value']['sha256']):
                self.release(key, job)
                await self.send(msg.reply, key, 'error', error='vision_invalid_request')
            else:
                job['task'] = asyncio.create_task(self.infer(key, job, msg.reply))
                def finished(task):
                    if not task.cancelled():
                        task.exception()
                    self.release(key, job)
                job['task'].add_done_callback(finished)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.warning('Vision request/reply unavailable; worker_id=%s', self.worker_id)

    async def infer(self, key, job, reply):
        # Reuse the established compute/play indicator without changing the
        # panel's activity schema. At most one VLM job runs on this worker.
        started = time.monotonic()
        try:
            seconds = min(12, max(0, (job['value']['deadline_ms'] - time.time() * 1000) / 1000))
            async with asyncio.timeout(seconds):
                text = await self.provider.response(job['value']['question'], bytes(job['image']))
            if not isinstance(text, str) or not text.strip() or len(text.encode()) > wire.MAX_TEXT:
                raise ValueError('Invalid vision result')
            await self.send(reply, key, 'ok', text=text)
            LOGGER.info('Vision completed; worker_id=%s elapsed_ms=%d', self.worker_id,
                        round((time.monotonic() - started) * 1000))
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.warning('Vision failed; worker_id=%s code=vision_provider_failed', self.worker_id)
            try:
                await self.send(reply, key, 'error', error='vision_provider_failed')
            except Exception:
                pass
        finally:
            self.release(key, job)

    async def stop(self):
        self.stopping = True
        tasks = [job['task'] for job in self.jobs.values() if job['task']]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for key, job in list(self.jobs.items()):
            self.release(key, job)

    async def close(self):
        await self.stop()
        if self.watchdog:
            self.watchdog.cancel()
            await asyncio.gather(self.watchdog, return_exceptions=True)
        await asyncio.wait_for(self.provider.close(), 5)
