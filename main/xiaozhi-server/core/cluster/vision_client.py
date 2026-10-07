"""Queue selection followed by acknowledged, targeted image chunks."""
import asyncio
import hashlib
import time
import uuid

from . import vision_protocol as wire


async def explain(client, revision, question, image, admitted=None):
    key, token = uuid.uuid4().hex, uuid.uuid4().hex
    worker = None
    base = {'protocol':wire.PROTOCOL, 'job_id':key, 'token':token}
    async def request(subject, data, timeout):
        reply = await client.request(subject, data, timeout=timeout)
        value = wire.response(reply.data, key)
        if worker is not None and value['worker_id'] != worker:
            raise ValueError('Vision worker identity differs')
        if value['status'] == 'error':
            raise ValueError(value['error'])
        return value
    try:
        async with asyncio.timeout(wire.SECONDS):
            value = {**base, 'op':'admit', 'revision':revision,
                     'deadline_ms':int(time.time() * 1000) + wire.SECONDS * 1000 - 100,
                     'size':len(image), 'sha256':hashlib.sha256(image).hexdigest(), 'question':question}
            wire.request(wire.encode(value, 4096))
            if not wire.jpeg(image):
                raise ValueError('Invalid camera JPEG')
            response = await request(wire.SUBJECT, wire.encode(value, 4096), 2)
            worker = response['worker_id']
            if response['status'] != 'admitted':
                raise ValueError('Vision admission unavailable')
            if admitted is not None:
                admitted(worker)
            subject = wire.target(worker)
            for offset in range(0, len(image), wire.CHUNK):
                payload = image[offset:offset + wire.CHUNK]
                response = await request(subject, key.encode() + token.encode() + offset.to_bytes(4, 'big') + payload, 2)
                if response['status'] != 'chunk' or response['offset'] != offset + len(payload):
                    raise ValueError('Vision image acknowledgement differs')
            result = await request(subject, wire.encode({**base, 'op':'finish'}, 4096), wire.SECONDS)
            if result['status'] != 'ok':
                raise ValueError('Vision inference unavailable')
            return result['text']
    finally:
        if worker is not None:
            # No retries/replay after uncertain delivery. Release upload/inference
            # on HTTP cancellation, MCP abort, error and successful completion.
            try:
                await asyncio.wait_for(client.publish(wire.target(worker),
                    wire.encode({**base, 'op':'cancel'}, 4096), reply='_INBOX.' + uuid.uuid4().hex), 1)
            except Exception:
                pass
