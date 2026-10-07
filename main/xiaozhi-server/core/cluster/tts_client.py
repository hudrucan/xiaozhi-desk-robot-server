"""Available-slot selection and unplayed-segment failover, never whole turns."""
import asyncio
import hashlib
import secrets
import time
import uuid
from dataclasses import dataclass

from . import tts_protocol as wire
from .llm_protocol import encode
from .worker_rpc import MAX_INFLIGHT, WorkerRpcError
from .tts_soundbank import SoundbankPlayback, OUTPUT_RATE


@dataclass(frozen=True)
class SegmentResult:
    index: int
    text: str
    pcm: bytes
    sample_rate: int
    worker_id: str
    synth_ms: int
    source: str = 'worker'


class ConnectionGuard:
    def __init__(self, rpc):
        self.rpc, self.failed = rpc, False

    def fail(self, code='tts_unavailable'):
        self.failed = True

    def require_live(self):
        if self.failed or self.rpc.stopping or not self.rpc.client.is_connected:
            raise WorkerRpcError('tts_unavailable')


class OwnedSegment:
    def __init__(self, rpc, bundle, worker_id, index, text, deadline_ms):
        self.rpc, self.bundle, self.worker_id = rpc, bundle, worker_id
        self.value = {'protocol': wire.PROTOCOL, 'core_id': rpc.core_id,
            'revision': bundle['revision'], 'fingerprint': bundle['fingerprint'],
            'job_id': uuid.uuid4().hex, 'token': uuid.uuid4().hex, 'index': index,
            'deadline_ms': deadline_ms}
        self.text, self.failure = text, None
        self.admitted = False

    def fail(self, code='tts_unavailable'):
        self.failure = code

    def require_live(self):
        if self.failure or self.rpc.stopping or not self.rpc.client.is_connected:
            raise WorkerRpcError('tts_unavailable')
        if time.time() * 1000 >= self.value['deadline_ms']:
            raise WorkerRpcError('tts_expired')

    async def call(self, op, **fields):
        self.require_live()
        value = {**self.value, 'op': op, **fields}
        payload = encode(value, wire.MAX_REQUEST)
        wire.request(payload)
        try:
            response = await asyncio.wait_for(self.rpc.client.request(wire.subject(self.worker_id), payload, timeout=1), 1.25)
            self.require_live()
            header, pcm = wire.parse_response(response.data, value, self.worker_id)
        except asyncio.CancelledError:
            raise
        except WorkerRpcError:
            raise
        except Exception:
            raise WorkerRpcError('tts_unavailable') from None
        if header['state'] == 'error':
            raise WorkerRpcError(header['error'])
        return header, pcm

    async def fetch(self):
        pcm = bytearray()
        metadata = None
        while True:
            header, chunk = await self.call('poll', offset=len(pcm))
            if header['state'] == 'pending' and not pcm:
                await asyncio.sleep(.1)
                continue
            if header['state'] != 'audio':
                raise WorkerRpcError('tts_invalid_result')
            current = tuple(header[key] for key in ('total_bytes', 'sample_rate', 'sha256', 'synth_ms'))
            if metadata is not None and metadata != current:
                raise WorkerRpcError('tts_invalid_result')
            metadata = current
            pcm.extend(chunk)
            if len(pcm) == header['total_bytes']:
                if hashlib.sha256(pcm).hexdigest() != header['sha256']:
                    raise WorkerRpcError('tts_invalid_result')
                self.require_live()
                return SegmentResult(self.value['index'], self.text, bytes(pcm), header['sample_rate'],
                    self.worker_id, header['synth_ms'])

    async def release(self):
        # Release remains best effort even after the deadline. No result or
        # cancel is accepted by a later job/attempt with different ownership.
        if self.rpc.client.is_connected:
            try:
                await asyncio.wait_for(self.rpc.client.request(wire.subject(self.worker_id),
                    encode({**self.value, 'op': 'cancel'}, wire.MAX_REQUEST), timeout=1), 1.25)
            except Exception:
                pass


class TTSPool:
    def __init__(self, rpc, bundle):
        self.rpc, self.bundle = rpc, bundle
        self.reserved, self.order = set(), {}
        self.lock = asyncio.Lock()
        self.counter = self.completed = self.failed = 0
        self.soundbank_hits = 0
        self.soundbank = SoundbankPlayback(bundle.get('soundbank'))
        self.soundbank.verify()

    def status(self):
        return {'protocol': wire.PROTOCOL, 'revision': self.bundle['revision'],
            'inflight': len(self.reserved), 'completed': self.completed, 'failed': self.failed,
            'soundbank_hits': self.soundbank_hits}

    async def idle(self, node):
        value = {'protocol': wire.PROTOCOL, 'op': 'status', 'core_id': self.rpc.core_id,
            'revision': self.bundle['revision'], 'fingerprint': self.bundle['fingerprint']}
        try:
            response = await asyncio.wait_for(self.rpc.client.request(wire.subject(node),
                encode(value, wire.MAX_REQUEST), timeout=.5), .75)
            header, pcm = wire.unpack(response.data)
            return (not pcm and type(header.get('revision')) is int and header == {'protocol': wire.PROTOCOL, 'worker_id': node,
                'revision': self.bundle['revision'], 'fingerprint': self.bundle['fingerprint'], 'state': 'idle'})
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    async def reserve(self, index, text, deadline, excluded, guard):
        while time.time() * 1000 < deadline:
            guard.require_live()
            async with self.lock:
                nodes = [node for node in self.bundle['workers'] if node not in self.reserved and node not in excluded]
                if not nodes and len(excluded) == len(self.bundle['workers']):
                    raise WorkerRpcError('tts_unavailable')
                readiness = await asyncio.gather(*(self.idle(node) for node in nodes))
                guard.require_live()
                candidates = [node for node, idle in zip(nodes, readiness) if idle]
                secrets.SystemRandom().shuffle(candidates)
                candidates.sort(key=lambda node: self.order.get(node, 0))
                for node in candidates:
                    owned = OwnedSegment(self.rpc, self.bundle, node, index, text, deadline)
                    self.rpc.streams.add(owned)
                    try:
                        header, _ = await owned.call('admit', text=text)
                        if header['state'] != 'admitted':
                            raise WorkerRpcError('tts_invalid_result')
                    except asyncio.CancelledError:
                        try:
                            await owned.release()
                        finally:
                            self.rpc.streams.discard(owned)
                        raise
                    except WorkerRpcError:
                        # An admission reply may be lost after the worker has
                        # reserved its slot. Cancel that unique attempt before
                        # trying another node; its lease is the second guard.
                        try:
                            await owned.release()
                        finally:
                            self.rpc.streams.discard(owned)
                        continue
                    self.reserved.add(node)
                    self.counter += 1
                    self.order[node] = self.counter
                    owned.admitted = True
                    return owned
            await asyncio.sleep(.1)
        raise WorkerRpcError('tts_expired')

    async def generate(self, index, text):
        if not isinstance(text, str) or not 0 < len(text.encode()) <= wire.MAX_TEXT or len(text) > 512:
            raise WorkerRpcError('tts_segment_too_large')
        if self.rpc.stopping or not self.rpc.client.is_connected:
            raise WorkerRpcError('tts_unavailable')
        if len(self.rpc.calls) >= MAX_INFLIGHT:
            raise WorkerRpcError('worker_rpc_busy')
        task = asyncio.current_task()
        guard = ConnectionGuard(self.rpc)
        self.rpc.streams.add(guard)
        self.rpc.calls.add(task)
        deadline = int(time.time() * 1000) + wire.MAX_SECONDS * 1000
        excluded = set()
        try:
            entry = self.soundbank.lookup(text)
            if entry is not None:
                try:
                    pcm = await self.soundbank.generate(entry)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A published cache hit must never silently synthesize a
                    # replacement voice when its pinned bytes become corrupt.
                    raise WorkerRpcError('tts_invalid_result') from None
                guard.require_live()
                self.completed += 1
                self.soundbank_hits += 1
                return SegmentResult(index, entry['text'] or text, pcm, OUTPUT_RATE,
                                     self.rpc.core_id, 0, source='soundbank')
            while True:
                guard.require_live()
                owned = await self.reserve(index, text, deadline, excluded, guard)
                self.rpc.streams.add(owned)
                try:
                    result = await owned.fetch()
                    self.completed += 1
                    return result
                except WorkerRpcError:
                    # Only fully buffered, unplayed results leave this method.
                    # Old attempt ownership is discarded before reassignment.
                    excluded.add(owned.worker_id)
                finally:
                    try:
                        await owned.release()
                    finally:
                        self.rpc.streams.discard(owned)
                        self.reserved.discard(owned.worker_id)
        except asyncio.CancelledError:
            raise
        except WorkerRpcError:
            self.failed += 1
            raise
        finally:
            self.rpc.streams.discard(guard)
            self.rpc.calls.discard(task)
