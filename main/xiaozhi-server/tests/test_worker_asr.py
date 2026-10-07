"""Offline ASR wire, queue routing and owned cancellation fixtures."""
import asyncio
import json
import sys
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from core.cluster import asr_protocol as wire
from core.cluster.asr_client import ASRClient
from core.cluster.asr_worker import ASRService
from core.cluster.voice_turn import VoiceTurn
from core.cluster.voice_diagnostics import VoiceDiagnostics
from core.cluster.worker_rpc import WorkerRpcError

AUDIO = {'format': 'opus', 'sample_rate': 16000, 'channels': 1, 'frame_duration': 60}


class Subscription:
    def __init__(self, bus, entry):
        self.bus, self.entry = bus, entry

    async def unsubscribe(self):
        self.bus.entries.remove(self.entry)


class Bus:
    is_connected = True

    def __init__(self):
        self.entries, self.published = [], []
        self.index = 0

    async def subscribe(self, subject, cb, queue='', **kwargs):
        entry = (subject, cb, queue)
        self.entries.append(entry)
        return Subscription(self, entry)

    async def flush(self):
        pass

    async def publish(self, subject, data, reply=''):
        self.published.append((subject, data))
        entries = [entry for entry in self.entries if entry[0] == subject]
        grouped = [entry for entry in entries if entry[2]]
        if grouped:
            entries = [grouped[self.index % len(grouped)]]
            self.index += 1
        for _, callback, _ in entries:
            await callback(SimpleNamespace(data=data, reply=reply))

    async def request(self, subject, data, timeout):
        inbox, future = '_INBOX.' + uuid.uuid4().hex, asyncio.get_running_loop().create_future()
        async def reply(message):
            if not future.done():
                future.set_result(message)
        subscription = await self.subscribe(inbox, cb=reply)
        try:
            await self.publish(subject, data, inbox)
            return await asyncio.wait_for(future, timeout)
        finally:
            await subscription.unsubscribe()


class Pipeline:
    instances = []

    def __init__(self, bundle, mode, partial):
        self.frames, self.closed, self.partial = [], False, partial
        self.instances.append(self)

    async def start(self):
        pass

    async def feed(self, data):
        self.frames.append(data)
        await self.partial('partial')
        return False

    async def finish(self):
        return 'transcript'

    async def close(self):
        self.closed = True


class ASRTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        Pipeline.instances = []
        self.bus, self.services = Bus(), []
        for node in ('deskb1x', 'deskb2x', 'deskb3x'):
            service = ASRService(self.bus, node, {'revision': 8}, Pipeline)
            await service.start()
            self.services.append(service)

    async def asyncTearDown(self):
        for service in self.services:
            await service.stop()

    def client(self, revision=8):
        async def partial(text):
            pass
        return ASRClient(self.bus, 'deskb1x', revision, AUDIO, 'manual', partial)

    async def wait_for_cleanup(self):
        # Terminal delivery and provider close are independently owned. Await
        # natural worker completion, without calling stop/cancel to hide a leak.
        tasks = [turn.task for service in self.services for turn in service.turns.values()]
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)

    async def until(self, predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(0.001)
        await asyncio.wait_for(wait(), timeout=2)

    def voice_fixture(self):
        sent, calls = [], []
        async def send(value):
            sent.append(value)
        async def generate(revision, dialogue, on_chunk, **options):
            calls.append(dialogue)
            return {'status': 'ok', 'text': 'answer'}
        rpc = SimpleNamespace(client=self.bus, core_id='deskb1x', generate_stream=generate)
        voice = VoiceTurn(rpc, 8, AUDIO, uuid.uuid4().hex, send, diagnostics=VoiceDiagnostics())
        return voice, sent, calls

    async def test_expired_asr_reopens_in_same_listening_turn_on_another_worker(self):
        original_feed = Pipeline.feed
        failures = []
        async def expire_once(instance, data):
            if not failures:
                failures.append(True)
                raise asyncio.TimeoutError
            return await original_feed(instance, data)
        voice, sent, calls = self.voice_fixture()
        with patch.object(Pipeline, 'feed', expire_once):
            await voice.start('auto')
            generation = voice.generation
            try:
                await voice.audio_frame(b'first')
                await self.until(lambda: sum(e['event'] == 'asr_admitted' for e in voice.diagnostics.events) == 2)
                workers = [e['worker_id'] for e in voice.diagnostics.events if e['event'] == 'asr_admitted']
                self.assertEqual(workers, ['deskb1x', 'deskb2x'])
                self.assertEqual(voice.generation, generation)
                self.assertEqual(voice.state, 'asr')
                self.assertFalse(calls)
                self.assertFalse(any(e.get('state') == 'complete' or e.get('end_conversation') for e in sent))
                await voice.audio_frame(b'second')
                await voice.stop()
                await asyncio.wait_for(voice.task, timeout=1)
                self.assertEqual(calls, [[{'role': 'user', 'content': 'transcript'}]])
                self.assertFalse(any(e.get('end_conversation') for e in sent))
                await self.wait_for_cleanup()
            finally:
                await voice.abort()

    async def test_empty_auto_result_reopens_without_completing_listening(self):
        finishes = []
        async def finish_once_empty(instance):
            finishes.append(True)
            return '' if len(finishes) == 1 else 'transcript'
        async def endpoint(instance, data):
            instance.frames.append(data)
            return True
        voice, sent, calls = self.voice_fixture()
        with patch.object(Pipeline, 'feed', endpoint), patch.object(Pipeline, 'finish', finish_once_empty):
            await voice.start('auto')
            try:
                await voice.audio_frame(b'first')
                await self.until(lambda: sum(e['event'] == 'asr_admitted' for e in voice.diagnostics.events) == 2)
                self.assertFalse(calls)
                self.assertFalse(any(e.get('state') == 'complete' for e in sent))
                await voice.audio_frame(b'second')
                await asyncio.wait_for(voice.task, timeout=1)
                self.assertEqual(len(calls), 1)
                await self.wait_for_cleanup()
            finally:
                await voice.abort()

    async def test_stop_during_reconnect_prevents_readmission_and_bounds_pending_audio(self):
        self.bus.is_connected = False
        voice, sent, calls = self.voice_fixture()
        await voice.start('manual')
        try:
            await self.until(lambda: voice.state == 'asr_restarting')
            for _ in range(100):
                await voice.audio_frame(b'frame')
            self.assertEqual(voice.queue.qsize(), 10)
            await voice.stop()
            self.bus.is_connected = True
            await asyncio.wait_for(voice.task, timeout=1)
            self.assertFalse(any(e['event'] == 'asr_admitted' for e in voice.diagnostics.events))
            self.assertFalse(calls)
            self.assertFalse(any(e.get('end_conversation') for e in sent))
        finally:
            await voice.abort()

    async def test_abort_during_reconnect_stops_owned_retry_task(self):
        self.bus.is_connected = False
        voice, sent, calls = self.voice_fixture()
        await voice.start('auto')
        await self.until(lambda: voice.state == 'asr_restarting')
        task = voice.task
        await voice.abort()
        self.bus.is_connected = True
        await asyncio.sleep(0.3)
        self.assertTrue(task.done())
        self.assertIsNone(voice.task)
        self.assertFalse(any(service.turns for service in self.services))
        self.assertFalse(calls)
        self.assertFalse(any(e.get('state') == 'complete' or e.get('end_conversation') for e in sent))

    async def test_queue_selects_one_worker_and_all_audio_is_targeted(self):
        clients = []
        for _ in range(3):
            queue = asyncio.Queue()
            for data in (b'first', b'second'):
                queue.put_nowait(('audio', data))
            queue.put_nowait(('end', b''))
            client = self.client()
            self.assertEqual(await client.transcribe(queue), 'transcript')
            clients.append(client)
        await self.wait_for_cleanup()
        self.assertEqual({client.worker_id for client in clients}, {'deskb1x', 'deskb2x', 'deskb3x'})
        self.assertTrue(all(instance.closed and instance.frames == [b'first', b'second'] for instance in Pipeline.instances))
        self.assertTrue(all(not service.turns for service in self.services))
        self.assertTrue(all(queue == wire.QUEUE_GROUP for subject, _, queue in self.bus.entries if subject == wire.OPEN_SUBJECT))
        self.assertTrue(all(not queue for subject, _, queue in self.bus.entries if subject.endswith('.input')))
        inputs = [wire.unpack(data)[0] for subject, data in self.bus.published if subject.endswith('.input')]
        self.assertFalse(any(value['kind'] == 'cancel' for value in inputs))

    async def test_terminal_result_does_not_wait_for_provider_cleanup(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original_close = Pipeline.close

        async def delayed_close(instance):
            entered.set()
            await release.wait()
            await original_close(instance)

        queue = asyncio.Queue()
        queue.put_nowait(('end', b''))
        with patch.object(Pipeline, 'close', delayed_close):
            try:
                self.assertEqual(await asyncio.wait_for(self.client().transcribe(queue), timeout=1), 'transcript')
                await asyncio.wait_for(entered.wait(), timeout=1)
                self.assertFalse(Pipeline.instances[0].closed)
                self.assertTrue(any(service.turns for service in self.services))
                inputs = [wire.unpack(data)[0] for subject, data in self.bus.published if subject.endswith('.input')]
                self.assertFalse(any(value['kind'] == 'cancel' for value in inputs))
            finally:
                release.set()
                await self.wait_for_cleanup()
        self.assertTrue(Pipeline.instances[0].closed)
        self.assertTrue(all(not service.turns for service in self.services))

    async def test_revision_mismatch_creates_no_provider(self):
        with self.assertRaisesRegex(WorkerRpcError, 'asr_revision_mismatch'):
            await self.client(9).transcribe(asyncio.Queue())
        self.assertFalse(Pipeline.instances)

    async def test_abort_closes_provider_and_owned_subscription(self):
        client = self.client()
        task = asyncio.create_task(client.transcribe(asyncio.Queue()))
        while not client.token:
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
        self.assertTrue(all(instance.closed for instance in Pipeline.instances))
        self.assertTrue(all(not service.turns for service in self.services))
        self.assertFalse(any(subject == client.inbox for subject, _, _ in self.bus.entries))
        self.assertTrue(any(wire.unpack(data)[0]['kind'] == 'cancel'
                            for subject, data in self.bus.published if subject.endswith('.input')))

    async def test_gap_fails_without_feeding_missing_speech(self):
        client = self.client()
        await client.open()
        client.seq = 1
        with self.assertRaises(Exception):
            await client.input('audio', b'gap')
        self.assertEqual(client.final.exception().code, 'asr_gap')
        await client.subscription.unsubscribe()
        await asyncio.sleep(0)
        self.assertFalse(any(instance.frames for instance in Pipeline.instances))

    async def test_wrong_token_cannot_cancel_turn(self):
        client = self.client()
        await client.open()
        service = next(service for service in self.services if client.turn_id in service.turns)
        turn = service.turns[client.turn_id]
        await service.input(SimpleNamespace(data=wire.packet(client.turn_id, uuid.uuid4().hex, 'cancel'), reply='_INBOX.' + uuid.uuid4().hex))
        self.assertFalse(turn.task.cancelled())
        await client.subscription.unsubscribe()

    async def test_lease_expiry_cancels_even_blocked_provider_start(self):
        async def blocked_start(instance):
            await asyncio.Event().wait()
        original = Pipeline.start
        Pipeline.start = blocked_start
        try:
            client = self.client()
            await client.open()
            service = next(service for service in self.services if client.turn_id in service.turns)
            service.turns[client.turn_id].lease = time.monotonic() - 10
            await asyncio.sleep(0.6)
            self.assertFalse(service.turns)
            self.assertTrue(Pipeline.instances[0].closed)
            self.assertEqual(client.final.exception().code, 'asr_unavailable')
            await client.subscription.unsubscribe()
        finally:
            Pipeline.start = original

    async def test_wire_rejects_oversize_duplicate_and_untrusted_subject(self):
        with self.assertRaises(ValueError):
            wire.packet(uuid.uuid4().hex, uuid.uuid4().hex, 'audio', audio=b'x' * 4097)
        with self.assertRaises(ValueError):
            wire.open_request(b'{"protocol":1,"protocol":2}')
        with self.assertRaises(ValueError):
            wire.input_subject('bad.*')

    async def test_core_voice_turn_emits_transcript_then_llm_without_tts(self):
        sent = []
        async def send(value):
            sent.append(value)
        async def generate_stream(revision, dialogue, on_chunk, **options):
            self.assertEqual(revision, 8)
            self.assertEqual(dialogue, [{'role': 'user', 'content': 'transcript'}])
            return {'status': 'ok', 'text': 'answer'}
        rpc = SimpleNamespace(client=self.bus, core_id='deskb1x', generate_stream=generate_stream)
        voice = VoiceTurn(rpc, 8, AUDIO, 'session', send)
        await voice.start('manual')
        await voice.audio_frame(b'frame')
        await voice.stop()
        await voice.task
        self.assertEqual([message['type'] for message in sent], ['stt', 'stt', 'stt', 'stt', 'llm', 'llm'])
        self.assertEqual(sent[-1]['state'], 'complete')
        self.assertFalse(any(message['type'] == 'tts' for message in sent))

    async def test_worker_entrypoint_import_does_not_import_server_runtime(self):
        before = set(sys.modules)
        __import__('worker_asr')
        forbidden = ('core.connection', 'core.websocket_server', 'core.http_server', 'core.providers.tts', 'core.providers.vad')
        self.assertFalse(any(name.startswith(forbidden) for name in set(sys.modules) - before))

    async def test_failed_voice_turn_still_emits_terminal_completion(self):
        sent=[]
        async def send(value): sent.append(value)
        async def generate(*_): self.fail('ASR revision failure must not reach LLM')
        rpc=SimpleNamespace(client=self.bus,core_id='deskb1x',generate_stream=generate)
        voice=VoiceTurn(rpc,9,AUDIO,'session',send)
        await voice.start('manual')
        await voice.task
        self.assertTrue(any(value.get('code')=='asr_revision_mismatch' for value in sent))
        self.assertEqual(sent[-1]['state'],'complete')
        self.assertFalse(any(value.get('end_conversation') for value in sent))

    async def test_disconnect_releases_admitted_worker_stream(self):
        client=self.client()
        await client.open()
        self.bus.is_connected=False
        await asyncio.sleep(0.6)
        self.assertTrue(all(not service.turns for service in self.services))
        self.assertTrue(all(instance.closed for instance in Pipeline.instances))
        await client.subscription.unsubscribe()
        client.final.cancel()


if __name__ == '__main__':
    unittest.main()
