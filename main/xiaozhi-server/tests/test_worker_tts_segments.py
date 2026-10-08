"""Offline segment distribution and ordering; no native model or real NATS."""
import asyncio
import copy
import hashlib
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from core.cluster import tts_protocol as wire
from core.cluster.activity import Activity
from core.cluster.nats_config import NatsConnectionConfig
from core.cluster.tts_config import DEFAULTS, fingerprint, validate_bundle
from core.cluster.tts_client import TTSPool, SegmentResult
from core.cluster.tts_segments import SegmentBuffer
from core.cluster.tts_turn import TTSTurn, MAX_LOOKAHEAD
from core.cluster.tts_worker import TTSService, Job
from core.cluster.worker_rpc import WorkerRPC, WorkerRpcError
from test_worker_llm_stream import Bus

NODES = ['deskb1x', 'deskb2x', 'deskb3x']


def bundle(node='deskb1x'):
    options = {**DEFAULTS, 'first_segment_chars': 0, 'split_on_all_punctuations': True, 'correct_words': []}
    files = {'model.onnx': 'a' * 64, 'tokens.txt': 'b' * 64, 'espeak-ng-data/fixture': 'c' * 64}
    return {'protocol': 'xiaozhi-worker-tts-config-v1', 'worker_id': node, 'revision': 8,
        'workers': NODES, 'model_root': '/fixture/models', 'options': options,
        'files': files, 'fingerprint': fingerprint(options, files)}


class Engine:
    def __init__(self):
        self.release = threading.Event()
        self.entered = threading.Event()
        self.active = self.peak = 0
        self.fail = False

    def generate(self, text):
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.entered.set()
        try:
            if not self.release.wait(3):
                raise RuntimeError('Fixture release deadline')
            if self.fail:
                raise RuntimeError('private-native-fixture')
            return bytes(3840), 16000
        finally:
            self.active -= 1


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(.01)


class DistributionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bus = Bus()
        self.rpc = WorkerRPC(NatsConnectionConfig(('nats://fixture:4222',), 'fixture', 'fixture-secret'),
            'deskb1x', client_factory=lambda: self.bus)
        self.pool = TTSPool(self.rpc, bundle())
        self.engines, self.workers = [], []
        for node in NODES:
            engine, activity = Engine(), Activity(None, node)
            activity.counts['tts'] = 0
            service = TTSService(self.bus, node, bundle(node), engine, activity)
            await service.start()
            self.engines.append(engine); self.workers.append(service)

    async def asyncTearDown(self):
        for engine in self.engines:
            engine.release.set()
        await self.rpc.close()
        for worker in self.workers:
            await worker.stop()

    async def test_three_segments_of_one_turn_reserve_three_distinct_slots(self):
        tasks = [asyncio.create_task(self.pool.generate(index, 'Segment,')) for index in range(3)]
        await until(lambda: all(engine.entered.is_set() for engine in self.engines))
        # Native synthesis starts before the final admission ACK reaches the
        # core. Wait for both sides of that handshake, not thread timing.
        await until(lambda: len(self.pool.reserved) == 3)
        self.assertEqual(len(self.pool.reserved), 3)
        self.assertTrue(all(engine.peak == 1 for engine in self.engines))
        for engine in self.engines:
            engine.release.set()
        results = await asyncio.gather(*tasks)
        self.assertEqual({result.worker_id for result in results}, set(NODES))
        self.assertEqual([result.index for result in results], [0, 1, 2])
        self.assertFalse(self.pool.reserved)
        self.assertFalse(self.rpc.calls)

    async def test_asr_busy_and_voice_mismatch_nodes_are_excluded(self):
        self.workers[0].activity.counts['asr'] = 1
        self.workers[1].bundle = {**bundle('deskb2x'), 'fingerprint': 'd' * 64}
        self.engines[2].release.set()
        result = await self.pool.generate(0, 'Hello,')
        self.assertEqual(result.worker_id, 'deskb3x')
        self.assertFalse(self.engines[0].entered.is_set())
        self.assertFalse(self.engines[1].entered.is_set())

    async def test_unplayed_native_failure_is_reassigned_to_another_worker(self):
        self.pool.order = {'deskb1x': 0, 'deskb2x': 1, 'deskb3x': 2}
        self.engines[0].fail = True
        for engine in self.engines:
            engine.release.set()
        result = await self.pool.generate(0, 'Hello,')
        self.assertNotEqual(result.worker_id, 'deskb1x')
        self.assertEqual(result.pcm, bytes(3840))
        self.assertEqual(result.index, 0)
        self.assertFalse(self.pool.reserved)

    async def test_cancel_keeps_native_slot_reserved_until_join(self):
        self.pool.order = {'deskb1x': 0, 'deskb2x': 1, 'deskb3x': 2}
        task = asyncio.create_task(self.pool.generate(0, 'Hello,'))
        await until(self.engines[0].entered.is_set)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertIsNotNone(self.workers[0].job)
        self.assertFalse(self.workers[0].available())
        self.engines[0].release.set()
        await until(lambda: self.workers[0].job is None)
        self.assertEqual(self.workers[0].activity.counts['tts'], 0)
        self.assertFalse(self.rpc.calls)
        self.assertFalse(self.rpc.streams)

    async def test_disconnect_while_waiting_for_slot_cannot_resume_old_segment(self):
        for worker in self.workers:
            worker.activity.counts['asr'] = 1
        task = asyncio.create_task(self.pool.generate(0, 'Hello,'))
        await until(lambda: bool(self.rpc.streams))
        await self.rpc._disconnected()
        await self.rpc._reconnected()
        for worker in self.workers:
            worker.activity.counts['asr'] = 0
        with self.assertRaisesRegex(WorkerRpcError, '^tts_unavailable$'):
            await task
        self.assertFalse(any(engine.entered.is_set() for engine in self.engines))
        self.assertFalse(self.rpc.streams)

    async def test_cancel_before_execute_starts_releases_unstarted_slot(self):
        worker = self.workers[0]
        job = worker.job = Job({})
        job.task = asyncio.create_task(worker.execute(job))
        job.task.add_done_callback(lambda task: worker.finished(job, task))
        job.task.cancel()
        await asyncio.gather(job.task, return_exceptions=True)
        self.assertIsNone(worker.job)
        self.assertEqual(worker.activity.counts['tts'], 0)
        self.assertFalse(self.engines[0].entered.is_set())

    async def test_corrupt_pcm_is_not_returned_as_a_playable_result(self):
        for engine in self.engines:
            engine.release.set()
        def corrupt(subject, payload):
            if subject.startswith('_INBOX.'):
                header, pcm = wire.unpack(payload)
                if pcm:
                    return wire.pack(header, bytes([1]) + pcm[1:])
            return payload
        self.bus.mutate = corrupt
        with self.assertRaisesRegex(WorkerRpcError, '^tts_unavailable$'):
            await self.pool.generate(0, 'Hello,')
        self.assertFalse(self.pool.reserved)

    async def test_every_admission_and_pull_has_owned_identity_and_bounded_pcm(self):
        for engine in self.engines:
            engine.release.set()
        await self.pool.generate(0, 'Hello,')
        for subject, payload in self.bus.published:
            if subject.startswith('_INBOX.'):
                _, pcm = wire.unpack(payload)
                self.assertLessEqual(len(pcm), wire.MAX_PCM_CHUNK)
        self.assertTrue(all(not queue for _, _, queue in self.bus.entries))


class Playback:
    def __init__(self, emit, send_audio):
        self.indices, self.started, self.finished = [], False, False
    async def segment(self, result):
        self.started = True
        self.indices.append(result.index)
    async def finish(self):
        self.finished, self.started = True, False
    async def close(self):
        pass


class TurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_out_of_order_completion_never_changes_playback_order_or_lookahead(self):
        gates, entered = [asyncio.Event() for _ in range(4)], asyncio.Queue()
        async def generate(index, text):
            await entered.put(index)
            await gates[index].wait()
            return SegmentResult(index, text, bytes(3840), 16000, NODES[index % 3], 10)
        pool = SimpleNamespace(bundle=bundle(), generate=generate)
        turn = TTSTurn(pool, None, None, playback_factory=Playback)
        try:
            for index, text in enumerate(('One, ', 'Two, ', 'Three, ')):
                turn.feed(text)
                self.assertEqual(await asyncio.wait_for(entered.get(), 1), index)
            turn.feed('Four.')
            finish = asyncio.create_task(turn.finish())
            self.assertEqual(len(turn.jobs), MAX_LOOKAHEAD)
            gates[2].set(); gates[1].set()
            await asyncio.sleep(.01)
            self.assertEqual(turn.playback.indices, [])
            self.assertTrue(entered.empty())
            gates[0].set()
            self.assertEqual(await asyncio.wait_for(entered.get(), 1), 3)
            gates[3].set()
            await finish
            self.assertEqual(turn.playback.indices, [0, 1, 2, 3])
            self.assertTrue(turn.playback.finished)
        finally:
            await turn.close()

    async def test_ahead_segment_failure_cancels_other_jobs_and_waiting_llm(self):
        entered, released = asyncio.Queue(), []
        async def generate(index, text):
            await entered.put(index)
            try:
                if index == 1:
                    raise WorkerRpcError('tts_provider_failed')
                await asyncio.Event().wait()
            finally:
                released.append(index)
        turn = TTSTurn(SimpleNamespace(bundle=bundle(), generate=generate), None, None, playback_factory=Playback)
        async def llm():
            turn.feed('One, ')
            await entered.get()
            turn.feed('Two, ')
            await asyncio.Event().wait()
        try:
            with self.assertRaisesRegex(WorkerRpcError, '^tts_provider_failed$'):
                await turn.wait_llm(llm())
            await asyncio.gather(turn.task, return_exceptions=True)
            self.assertEqual(set(released), {0, 1})
            self.assertFalse(turn.playback.indices)
            self.assertFalse(turn.jobs)
        finally:
            await turn.close()

    def test_splitter_retains_numeric_and_first_sentence_rules(self):
        options = {**DEFAULTS, 'first_segment_chars': 45, 'split_on_all_punctuations': True}
        buffer = SegmentBuffer(options)
        buffer.append('Voltage is 3.3 V, and the version is 1.2.3. ')
        segment = buffer.pop()
        self.assertIn('3.3 V', segment)
        buffer.finished = True
        remaining = buffer.pop()
        self.assertEqual((segment + ' ' + (remaining or '')).strip(),
            'Voltage is 3.3 V, and the version is 1.2.3.')
        self.assertIsNone(buffer.pop())

    def test_bundle_voice_fingerprint_and_wire_bounds(self):
        value = bundle()
        validate_bundle(value, 'deskb1x')
        self.assertEqual(value['fingerprint'], fingerprint(
            dict(reversed(list(value['options'].items()))), dict(reversed(list(value['files'].items())))))
        changed = {**value, 'options': {**value['options'], 'speaker_id': 1}}
        with self.assertRaises(ValueError):
            validate_bundle(changed, 'deskb1x')
        owned = {'protocol': wire.PROTOCOL, 'op': 'poll', 'core_id': 'deskb1x', 'revision': 8,
            'fingerprint': value['fingerprint'], 'job_id': 'a' * 32, 'token': 'b' * 32,
            'index': 0, 'deadline_ms': int(time.time() * 1000) + 1000, 'offset': 0}
        pcm = bytes(3840)
        header = wire.response_header(owned, 'deskb2x', 'audio', offset=0, total_bytes=len(pcm),
            sample_rate=16000, sha256=hashlib.sha256(pcm).hexdigest(), synth_ms=10)
        self.assertEqual(wire.parse_response(wire.pack(header, pcm), owned, 'deskb2x')[1], pcm)
        with self.assertRaises(ValueError):
            wire.parse_response(wire.pack({**header, 'token': 'c' * 32}, pcm), owned, 'deskb2x')
        with self.assertRaises(ValueError):
            wire.pack(header, bytes(wire.MAX_PCM_CHUNK + 2))


class EncoderTests(unittest.TestCase):
    def test_resampling_and_frame_padding_span_segment_boundaries(self):
        import audioop
        from core.cluster.tts_playback import PCMEncoder
        class Opus:
            def __init__(self):
                self.frames = []
            def encode(self, frame, size):
                self.frames.append(frame)
                return b'fixture-opus'
        encoder = PCMEncoder.__new__(PCMEncoder)
        encoder.audioop, encoder.encoder = audioop, Opus()
        encoder.buffer, encoder.resample, encoder.source_rate = bytearray(), None, None
        samples = b'\x01\x00' * 6000
        encoder.encode(samples[:5000], 22050)
        encoder.encode(samples[5000:], 22050)
        encoder.encode(b'', 22050, final=True)
        expected, _ = audioop.ratecv(samples, 2, 1, 22050, 16000, None)
        expected += bytes((-len(expected)) % 1920)
        self.assertEqual(b''.join(encoder.encoder.frames), expected)
        self.assertFalse(encoder.buffer)
        encoder.encode(bytes(1920), 16000)
        encoder.encode(bytes(2400), 24000)
        encoder.encode(b'', 24000, final=True)
        converted, _ = audioop.ratecv(bytes(2400), 2, 1, 24000, 16000, None)
        tail = bytes(1920) + converted
        tail += bytes((-len(tail)) % 1920)
        self.assertEqual(b''.join(encoder.encoder.frames), expected + tail)


class ExportTests(unittest.TestCase):
    def test_cache_only_export_pins_assets_without_mutating_cloud_or_resolving_secrets(self):
        from export_worker_tts_config import build_bundle
        from export_worker_llm_config import write_private
        from core.cluster.tts_config import MAX_BUNDLE, load_bundle, verify_assets
        config = {'selected_module': {'TTS': 'Local'},
            'TTS': {'Local': {**DEFAULTS, 'model_dir': 'models'}},
            'LLM': {'unused': {'api_key': '${secret:UNUSED}'}},
            'static_soundbank': {'enabled': False}}
        snapshot = {'payload': {'manifest': {'revision': 8}, 'object': {'schema_version': 2,
            'layers': {key: {} for key in ('global', 'environments', 'roles', 'cluster', 'nodes')}}}}
        store = SimpleNamespace(bootstrap={'node_id': 'deskb1x'}, _read_cache=lambda _: snapshot,
            _resolve=lambda _: (config, {}), secrets=SimpleNamespace(resolve=lambda _: self.fail('No key resolution')))
        before = copy.deepcopy(config)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / 'models'
            (model / 'espeak-ng-data').mkdir(parents=True)
            for name in ('model.onnx', 'tokens.txt', 'espeak-ng-data/fixture'):
                (model / name).write_bytes(name.encode())
            value = build_bundle(store, root, 8, NODES)
            self.assertEqual(config, before)
            verify_assets(value)
            path = root / 'bundle.json'
            self.assertTrue(write_private(path, value, max_bytes=MAX_BUNDLE))
            self.assertFalse(write_private(path, value, max_bytes=MAX_BUNDLE))
            self.assertEqual(load_bundle(str(path), 'deskb1x'), value)
            with self.assertRaises(ValueError):
                build_bundle(store, root, 9, NODES)
            config['static_soundbank']['enabled'] = True
            with self.assertRaises(ValueError):
                build_bundle(store, root, 8, NODES)
            config['static_soundbank']['enabled'] = False
            (model / 'tokens.txt').write_bytes(b'changed')
            with self.assertRaises(ValueError):
                verify_assets(value)
            (model / 'model.onnx').unlink()
            with self.assertRaises(ValueError):
                build_bundle(store, root, 8, NODES)


class VoiceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_voice_tts_has_one_lifecycle_and_preserves_final_text(self):
        from core.cluster.voice_turn import VoiceTurn
        messages, audio = [], []
        async def emit(value):
            messages.append(value)
        async def send_audio(value):
            audio.append(value)
        class Output(Playback):
            def __init__(self, emit, send):
                super().__init__(emit, send)
                self.emit, self.send, self.packet_count = emit, send, 0
            async def segment(self, result):
                if not self.started:
                    self.started = True
                    await self.emit({'type': 'tts', 'state': 'start'})
                self.indices.append(result.index)
                await self.send(b'fixture-audio')
                self.packet_count += 1
            async def finish(self):
                if self.started:
                    await self.emit({'type': 'tts', 'state': 'stop'})
                await super().finish()
        async def generate(index, text):
            return SegmentResult(index, text, bytes(3840), 16000, 'deskb2x', 1)
        async def llm(revision, dialogue, on_chunk, **options):
            await on_chunk('Hello, ', 0)
            await on_chunk('world.', 1)
            return {'status': 'ok', 'text': 'Hello, world.'}
        async def transcript(queue):
            return 'Question'
        pool = SimpleNamespace(bundle=bundle(), generate=generate)
        rpc = SimpleNamespace(client=None, core_id='deskb1x', generate_stream=llm)
        voice = VoiceTurn(rpc, 8, {}, 'fixture-session', emit, pool, send_audio)
        def turn(pool, emit, send):
            return TTSTurn(pool, emit, send, playback_factory=Output)
        with patch('core.cluster.voice_turn.ASRClient', return_value=SimpleNamespace(transcribe=transcript)), \
                patch('core.cluster.tts_turn.TTSTurn', side_effect=turn):
            await voice.run(0, asyncio.Queue(), 'manual')
        self.assertEqual([m['state'] for m in messages if m['type'] == 'tts'], ['start', 'stop'])
        self.assertEqual([m['text'] for m in messages if m['type'] == 'llm' and m.get('state') == 'final'], ['Hello, world.'])
        self.assertFalse(messages[-1]['text_only'])
        self.assertTrue(audio)
        self.assertIsNone(voice.current_tts)


if __name__ == '__main__':
    unittest.main()
