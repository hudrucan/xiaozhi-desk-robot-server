"""Offline ASR wire, queue routing and owned cancellation fixtures."""
import asyncio
import json
import sys
import time
import unittest
import uuid
from types import SimpleNamespace

from core.cluster import asr_protocol as wire
from core.cluster.asr_client import ASRClient
from core.cluster.asr_worker import ASRService
from core.cluster.voice_turn import VoiceTurn
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
        self.assertEqual({client.worker_id for client in clients}, {'deskb1x', 'deskb2x', 'deskb3x'})
        self.assertTrue(all(instance.closed and instance.frames == [b'first', b'second'] for instance in Pipeline.instances))
        self.assertTrue(all(not service.turns for service in self.services))
        self.assertTrue(all(queue == wire.QUEUE_GROUP for subject, _, queue in self.bus.entries if subject == wire.OPEN_SUBJECT))
        self.assertTrue(all(not queue for subject, _, queue in self.bus.entries if subject.endswith('.input')))

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
        async def generate_stream(revision, dialogue, on_chunk):
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
