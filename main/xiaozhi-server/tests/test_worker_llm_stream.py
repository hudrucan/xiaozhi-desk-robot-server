"""Offline streaming fixtures: order, ACK backpressure and turn ownership."""
import asyncio
import json
import unittest
import uuid
from types import SimpleNamespace

from core.cluster import llm_protocol as legacy
from core.cluster import llm_stream_protocol as wire
from core.cluster.llm_worker import LLMWorker
from core.cluster.nats_config import NatsConfig, NatsConnectionConfig
from core.cluster.worker_rpc import WorkerRPC, WorkerRpcError


class Subscription:
    def __init__(self, bus, entry):
        self.bus, self.entry = bus, entry

    async def unsubscribe(self):
        self.bus.entries.remove(self.entry)


class Bus:
    is_connected = True
    is_closed = False

    def __init__(self):
        self.entries, self.published = [], []
        self.mutate = None

    async def subscribe(self, subject, cb, queue='', **options):
        entry = (subject, cb, queue)
        self.entries.append(entry)
        return Subscription(self, entry)

    async def flush(self):
        pass

    async def publish(self, subject, data, reply=''):
        self.published.append((subject, data))
        if self.mutate:
            data = self.mutate(subject, data)
        for topic, callback, _ in list(self.entries):
            if topic == subject:
                await callback(SimpleNamespace(data=data, reply=reply))

    async def request(self, subject, data, timeout):
        inbox = '_INBOX.' + uuid.uuid4().hex
        result = asyncio.get_running_loop().create_future()
        async def received(message):
            if not result.done():
                result.set_result(message)
        subscription = await self.subscribe(inbox, cb=received)
        try:
            await self.publish(subject, data, reply=inbox)
            return await asyncio.wait_for(result, timeout)
        finally:
            await subscription.unsubscribe()

    async def drain(self):
        self.is_closed = True

    async def close(self):
        self.is_closed = True


class Provider:
    def __init__(self):
        self.parts = ['Hello', ' world']
        self.release = None
        self.closed = 0
        self.after_first = asyncio.Event()
        self.error = False

    async def response_text_async(self, messages):
        try:
            for index, part in enumerate(self.parts):
                yield part
                if index == 0:
                    self.after_first.set()
                    if self.release:
                        await self.release.wait()
                if self.error:
                    raise RuntimeError('private-provider-fixture')
        finally:
            self.closed += 1


class LLMStreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bus, self.provider = Bus(), Provider()
        config = NatsConfig(('nats://fixture:4222',), 'fixture', 'fixture-secret', 'deskb2x')
        self.worker = LLMWorker(config, asyncio.Event(), {'revision': 8, 'prompt': 'Configured prompt'}, self.provider)
        self.worker.client = self.bus
        await self.bus.subscribe(wire.SUBJECT, cb=self.worker._receive_stream, queue=wire.QUEUE_GROUP)
        await self.bus.subscribe(legacy.SUBJECT, cb=self.worker._receive, queue=legacy.QUEUE_GROUP)
        await self.bus.subscribe(legacy.CANCEL_SUBJECT, cb=self.worker._cancel)
        self.rpc = WorkerRPC(NatsConnectionConfig(config.servers, config.username, config.password),
            'deskb1x', client_factory=lambda: self.bus)
        self.received = []

    async def asyncTearDown(self):
        await self.rpc.close()
        await self.worker._shutdown()

    async def chunk(self, text, seq):
        self.received.append((seq, text))

    def call(self, revision=8, seconds=30, callback=None):
        return self.rpc.generate_stream(revision, [{'role': 'user', 'content': 'Hello'}],
            callback or self.chunk, seconds)

    async def test_first_chunk_arrives_before_provider_finishes_and_final_rpc_is_unchanged(self):
        self.provider.release = asyncio.Event()
        early = asyncio.Event()
        async def consume(text, seq):
            await self.chunk(text, seq)
            early.set()
        task = asyncio.create_task(self.call(callback=consume))
        await asyncio.wait_for(early.wait(), 1)
        self.assertFalse(task.done())
        self.assertEqual(self.received, [(0, 'Hello')])
        self.provider.release.set()
        result = await task
        self.assertEqual(result['text'], 'Hello world')
        self.assertEqual(self.received, [(0, 'Hello'), (1, ' world')])
        self.assertEqual(len(self.bus.entries), 3)
        self.assertEqual(self.rpc.status()['inflight'], 0)
        self.assertEqual((await self.rpc.generate(8, [{'role': 'user', 'content': 'Hello'}]))['protocol'], legacy.PROTOCOL)

    async def test_consumer_backpressure_stops_provider_advancing(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def consume(text, seq):
            if seq == 0:
                entered.set()
                await release.wait()
            await self.chunk(text, seq)
        task = asyncio.create_task(self.call(callback=consume))
        await asyncio.wait_for(entered.wait(), 1)
        self.assertFalse(self.provider.after_first.is_set())
        release.set()
        await task
        await asyncio.gather(*(job for _, job in list(self.worker.jobs.values())))
        self.assertEqual(self.provider.closed, 1)

    async def test_large_unicode_provider_chunk_preserves_every_byte(self):
        self.provider.parts = ['\u1ebf' * 3000]
        result = await self.call()
        self.assertEqual(result['text'], '\u1ebf' * 3000)
        self.assertEqual(''.join(text for _, text in self.received), result['text'])
        self.assertTrue(all(len(text.encode()) <= wire.MAX_CHUNK_BYTES for _, text in self.received))

    async def test_gap_duplicate_worker_switch_and_bad_digest_fail_without_final_success(self):
        for field, change in (('seq', 2), ('seq', 0), ('worker_id', 'deskb3x'), ('sha256', '0' * 64)):
            self.received.clear()
            def mutate(subject, data):
                value = json.loads(data)
                target = 'complete' if field == 'sha256' else 'chunk'
                if value.get('kind') == target:
                    value[field] = change
                return legacy.encode(value, wire.MAX_EVENT_BYTES)
            self.bus.mutate = mutate
            with self.assertRaisesRegex(WorkerRpcError, '^worker_rpc_invalid_reply$'):
                await self.call()
            await asyncio.gather(*(task for _, task in list(self.worker.jobs.values())))
            self.bus.mutate = None
        self.assertEqual(self.rpc.status()['completed'], 0)

    async def test_cancel_closes_provider_and_removes_owned_subscription(self):
        self.provider.release = asyncio.Event()
        task = asyncio.create_task(self.call())
        await asyncio.wait_for(self.provider.after_first.wait(), 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.gather(*(job for _, job in list(self.worker.jobs.values())))
        self.assertEqual(self.provider.closed, 1)
        self.assertEqual(len(self.bus.entries), 3)
        self.assertFalse(self.rpc.calls)
        self.assertFalse(self.rpc.streams)
        self.assertFalse(self.worker.jobs)

    async def test_disconnect_fails_owned_stream_and_reconnect_does_not_resume_it(self):
        self.provider.release = asyncio.Event()
        task = asyncio.create_task(self.call())
        await asyncio.wait_for(self.provider.after_first.wait(), 1)
        self.bus.is_connected = False
        await self.rpc._disconnected()
        await self.worker._disconnected()
        with self.assertRaisesRegex(WorkerRpcError, '^llm_stream_unavailable$'):
            await task
        await asyncio.gather(*(job for _, job in list(self.worker.jobs.values())))
        self.bus.is_connected = True
        await self.rpc._reconnected()
        self.provider.release.set()
        self.received.clear()
        self.assertEqual((await self.call())['text'], 'Hello world')
        self.assertEqual(self.received, [(0, 'Hello'), (1, ' world')])

    async def test_deadline_partial_failure_and_revision_rejection(self):
        with self.assertRaisesRegex(WorkerRpcError, '^llm_revision_mismatch$'):
            await self.call(revision=9)
        self.assertEqual(self.provider.closed, 0)
        self.provider.error = True
        with self.assertRaisesRegex(WorkerRpcError, '^llm_provider_failed$'):
            await self.call()
        self.assertEqual(self.received, [(0, 'Hello')])
        self.assertNotIn('private-provider-fixture', repr(self.bus.published))
        self.provider.error = False
        self.provider.release = asyncio.Event()
        with self.assertRaisesRegex(WorkerRpcError, '^llm_expired$'):
            await self.call(seconds=1)

    async def test_total_output_limit_never_delivers_oversize_chunk(self):
        self.provider.parts = ['x' * (legacy.MAX_TEXT_BYTES + 1)]
        with self.assertRaisesRegex(WorkerRpcError, '^llm_output_too_large$'):
            await self.call()
        self.assertFalse(self.received)

    async def test_shutdown_cancels_stream_and_releases_core_admission(self):
        self.provider.release = asyncio.Event()
        task = asyncio.create_task(self.call())
        await asyncio.wait_for(self.provider.after_first.wait(), 1)
        await self.rpc.close()
        self.assertTrue(task.cancelled())
        await asyncio.gather(*(job for _, job in list(self.worker.jobs.values())))
        self.assertFalse(self.worker.jobs)
        self.assertFalse(self.rpc.calls)
        self.assertFalse(self.rpc.streams)
        self.assertEqual(self.provider.closed, 1)

    async def test_malformed_and_oversize_events_fail_before_consumer(self):
        for payload in (b'\xff', b'x' * (wire.MAX_EVENT_BYTES + 1)):
            self.bus.mutate = lambda subject, data: payload if json.loads(data).get('kind') == 'chunk' else data
            with self.assertRaisesRegex(WorkerRpcError, '^worker_rpc_invalid_reply$'):
                await self.call()
            await asyncio.gather(*(job for _, job in list(self.worker.jobs.values())))
        self.assertFalse(self.received)
        self.bus.mutate = None

    async def test_disconnect_during_completion_ack_cannot_be_reported_as_success(self):
        publish = self.bus.publish
        async def interrupted(subject, data, reply=''):
            value = json.loads(data)
            await publish(subject, data, reply)
            if (value.get('protocol') == wire.PROTOCOL and 'kind' not in value
                    and value.get('seq') == 3):
                # Both connections may be up again when publish returns. The
                # remembered disconnect must still invalidate this old turn.
                await self.rpc._disconnected()
                await self.rpc._reconnected()
        self.bus.publish = interrupted
        with self.assertRaisesRegex(WorkerRpcError, '^llm_stream_unavailable$'):
            await self.call()
        self.assertEqual(self.rpc.status()['completed'], 0)

    def test_stream_event_bounds_and_ack_correlation(self):
        request = {'request_id': 'a' * 32, 'revision': 8}
        event = wire.event(request, 'deskb2x', 'chunk', 1, text='Hello')
        value = wire.parse_event(event, request['request_id'], 8)
        wire.validate_ack(wire.ack(value), value)
        for data in (b'\xff', b'x' * (wire.MAX_EVENT_BYTES + 1), b'{"seq":1,"seq":2}'):
            with self.assertRaises((ValueError, UnicodeError)):
                wire.parse_event(data, request['request_id'], 8)
        with self.assertRaises(ValueError):
            wire.event(request, 'deskb2x', 'chunk', 1, text='x' * (wire.MAX_CHUNK_BYTES + 1))
        value['seq'] = 2
        with self.assertRaises(ValueError):
            wire.validate_ack(wire.ack(value), wire.parse_event(event, request['request_id'], 8))
        value['seq'] = True
        with self.assertRaises(ValueError):
            wire.validate_ack(wire.ack(value), wire.parse_event(event, request['request_id'], 8))


if __name__ == '__main__':
    unittest.main()
