"""Pinned audio, mixed ordering and owned cancellation; no model/Cloud/NATS."""
import asyncio
import copy
import io
import json
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from config.config_store import canonical_bytes, checksum
from core.cluster.tts_client import TTSPool, SegmentResult
from core.cluster.tts_config import validate_bundle
from core.cluster.tts_segments import SegmentBuffer
from core.cluster.tts_soundbank import SoundbankPlayback, decode_audio, fingerprint, validate
from core.cluster.tts_turn import TTSTurn
from core.cluster.worker_rpc import WorkerRpcError
from export_worker_tts_config import build_bundle
from provision_core_soundbank import provision
from test_worker_tts_segments import bundle, until, NODES


def wav(rate=24000, channels=1):
    output = io.BytesIO()
    with wave.open(output, 'wb') as stream:
        stream.setparams((channels, 2, rate, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\xe8\x03' * (rate * channels * 60 // 1000))
    return output.getvalue()


class Fixture:
    def __init__(self, root):
        self.root = root.resolve()
        self.cache = self.root / 'data/cloud-soundbank/objects'
        self.cache.mkdir(parents=True)
        model = self.root / 'models'
        (model / 'espeak-ng-data').mkdir(parents=True)
        for name in ('model.onnx', 'tokens.txt', 'espeak-ng-data/fixture'):
            (model / name).write_bytes(name.encode())
        content = wav()
        pointer = {'file_id': 'private-drive-blob', 'size': len(content), 'sha256': checksum(content)}
        self.asset = self.cache / (pointer['sha256'] + '.wav')
        self.asset.write_bytes(content)
        self.asset.chmod(0o600)
        self.config = {'selected_module': {'TTS': 'Local'},
            'TTS': {'Local': {**bundle()['options'], 'model_dir': 'models'}},
            'static_soundbank': {'enabled': True, 'directory': 'data/soundbank',
                'entries': {'Hello': {'file': 'hello.wav', 'text': 'Recorded greeting', 'cloud': pointer}}},
            'xiaozhi': {'audio_params': {'sample_rate': 16000}}}
        snapshot = {'payload': {'manifest': {'revision': 8}, 'object': {'schema_version': 2,
            'layers': {key: {} for key in ('global', 'environments', 'roles', 'cluster', 'nodes')}}}}
        self.store = SimpleNamespace(bootstrap={'node_id': 'deskb1x'},
            soundbank_assets=SimpleNamespace(cache_dir=self.cache),
            _read_cache=lambda _: snapshot, _resolve=lambda _: (self.config, {}))
        self.ready()

    def ready(self, revision=8):
        subset = {'static_soundbank': copy.deepcopy(self.config['static_soundbank']),
                  'xiaozhi': self.config['xiaozhi']}
        index = {'protocol': 'xiaozhi-soundbank-cache-v1', 'node_id': 'deskb1x',
            'revision': revision, 'configuration': subset, 'fingerprint': checksum(canonical_bytes(subset))}
        path = self.cache.parent / 'ready.json'
        path.write_bytes(canonical_bytes(index))
        path.chmod(0o600)

    def export(self):
        return build_bundle(self.store, self.root, 8, NODES)


class ExportAndProvisionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.fixture = Fixture(Path(self.directory.name))

    def test_export_requires_verified_ready_cache_and_strips_cloud_id(self):
        before = copy.deepcopy(self.fixture.config)
        value = self.fixture.export()
        self.assertFalse((self.fixture.root / 'data/soundbank').exists())
        self.assertNotIn('private-drive-blob', json.dumps(value))
        self.assertNotIn('directory', value['soundbank'])
        self.assertEqual(self.fixture.config, before)
        self.assertEqual(value['soundbank']['revision'], 8)
        self.assertEqual(validate_bundle(value, 'deskb1x'), value)
        self.fixture.ready(9)
        with self.assertRaises(ValueError):
            self.fixture.export()

    def test_changed_config_or_missing_corrupt_blob_blocks_export(self):
        self.fixture.config['static_soundbank']['entries']['Hello']['text'] = 'New recording'
        with self.assertRaises(ValueError):
            self.fixture.export()
        self.fixture.ready()
        self.fixture.asset.write_bytes(b'corrupt')
        with self.assertRaises(ValueError):
            self.fixture.export()
        self.fixture.asset.unlink()
        with self.assertRaises(OSError):
            self.fixture.export()

    def test_private_core_copy_survives_source_removal_and_is_idempotent(self):
        source = self.fixture.export()
        destination = self.fixture.root / 'private-core-audio'
        destination.mkdir(mode=0o700)
        pinned, changed = provision(source, self.fixture.cache, destination)
        self.assertTrue(changed)
        self.assertEqual(pinned['fingerprint'], source['fingerprint'])
        self.assertNotEqual(pinned['soundbank']['root'], source['soundbank']['root'])
        again, changed = provision(source, self.fixture.cache, destination)
        self.assertFalse(changed)
        self.assertEqual(again, pinned)
        self.fixture.asset.unlink()
        playback = SoundbankPlayback(pinned['soundbank'])
        playback.verify()
        self.assertEqual(len(playback.decode(playback.lookup('HELLO!'))), 1920)
        self.assertEqual(len(list(destination.iterdir())), 1)

    def test_bad_source_never_publishes_partial_cache_or_replaces_prior_cache(self):
        source = self.fixture.export()
        destination = self.fixture.root / 'private-core-audio'
        destination.mkdir(mode=0o700)
        original = self.fixture.asset.read_bytes()
        self.fixture.asset.write_bytes(b'corrupt')
        with self.assertRaises(ValueError):
            provision(source, self.fixture.cache, destination)
        self.assertEqual(list(destination.iterdir()), [])
        self.fixture.asset.write_bytes(original)
        pinned, _ = provision(source, self.fixture.cache, destination)
        newer = copy.deepcopy(source)
        newer['soundbank']['entries'][0]['text'] = 'Changed recording transcript'
        newer['soundbank']['fingerprint'] = fingerprint(newer['soundbank']['entries'])
        self.fixture.asset.write_bytes(b'corrupt')
        with self.assertRaises(ValueError):
            provision(newer, self.fixture.cache, destination)
        SoundbankPlayback(pinned['soundbank']).verify()
        self.assertEqual(len(list(destination.iterdir())), 1)

    def test_paths_private_permissions_duplicate_identity_and_revision_are_strict(self):
        value = self.fixture.export()['soundbank']
        self.fixture.asset.chmod(0o644)
        with self.assertRaises(ValueError):
            SoundbankPlayback(value).verify()
        self.fixture.asset.chmod(0o600)
        original = self.fixture.asset.read_bytes()
        self.fixture.asset.unlink()
        target = self.fixture.root / 'outside.wav'
        target.write_bytes(original)
        self.fixture.asset.symlink_to(target)
        with self.assertRaises(ValueError):
            SoundbankPlayback(value).verify()
        for key, bad in (('name', '../outside.wav'), ('sample_rate', 16000)):
            tampered = copy.deepcopy(value)
            tampered['entries'][0]['canonical'][key] = bad
            tampered['fingerprint'] = fingerprint(tampered['entries'])
            with self.assertRaises(ValueError):
                validate(tampered, 8)
        with self.assertRaises(ValueError):
            validate(value, 9)
        tampered = copy.deepcopy(value)
        tampered['entries'].append(copy.deepcopy(tampered['entries'][0]))
        tampered['entries'][1]['canonical']['size'] += 1
        tampered['fingerprint'] = fingerprint(tampered['entries'])
        with self.assertRaises(ValueError):
            validate(tampered, 8)

    def test_compatible_optimized_p3_is_decoded_without_provider_or_gain_processing(self):
        import audioop
        from opuslib_next import Encoder, constants
        from core.utils.p3 import encode_opus_packets
        content = encode_opus_packets([Encoder(16000, 1, constants.APPLICATION_AUDIO).encode(bytes(1920), 960)])
        pointer = {'file_id': 'private-optimized-blob', 'sha256': checksum(content), 'size': len(content)}
        path = self.fixture.cache / (pointer['sha256'] + '.p3')
        path.write_bytes(content)
        path.chmod(0o600)
        self.fixture.config['static_soundbank']['entries']['Hello']['optimized'] = {
            'file': 'hello.p3', 'codec': 'opus', 'channels': 1,
            'sample_rate': 16000, 'frame_duration_ms': 60, 'cloud': pointer}
        self.fixture.ready()
        playback = SoundbankPlayback(self.fixture.export()['soundbank'])
        playback.verify()
        entry = playback.lookup('Hello')
        self.assertEqual(len(playback.decode(entry)), 1920)
        self.assertLess(audioop.max(playback.decode(entry), 2), 10)
        # Incompatible optimized rate uses the canonical recording as before.
        entry['optimized']['sample_rate'] = 24000
        self.assertEqual(len(playback.decode(entry)), 1920)
        self.assertGreater(audioop.max(playback.decode(entry), 2), 10)

    def test_mp3_decoder_is_offline_bounded_and_rejects_truncated_overlong_output(self):
        asset = {'name': 'a' * 64 + '.mp3'}
        with patch('core.cluster.tts_soundbank.subprocess.run', return_value=SimpleNamespace(stdout=bytes(1920))) as run:
            self.assertEqual(decode_audio(b'fixture-mp3', asset), bytes(1920))
            argv = run.call_args.args[0]
            self.assertEqual(argv[argv.index('-protocol_whitelist') + 1], 'pipe')
            self.assertEqual(argv[argv.index('-f') + 1], 'mp3')
            self.assertEqual(run.call_args.kwargs['timeout'], 15)
        with patch('core.cluster.tts_soundbank.subprocess.run', return_value=SimpleNamespace(stdout=bytes(16000 * 2 * 30 + 2))):
            with self.assertRaises(ValueError):
                decode_audio(b'fixture-mp3', asset)

    def test_first_segment_policy_preserves_known_cached_prefix(self):
        value = self.fixture.export()
        phrase = 'A longer recorded greeting'
        options = {**value['options'], 'first_segment_chars': 8}
        buffer = SegmentBuffer(options, {phrase.casefold(): {}})
        buffer.append('A longer recorded ')
        self.assertIsNone(buffer.pop())
        buffer.append('greeting!')
        self.assertEqual(buffer.pop(), phrase + '!')

    def test_native_benchmark_rejects_recorded_phrases_without_connecting(self):
        from probe_worker_tts_segments import require_native_segments
        value = self.fixture.export()
        with self.assertRaises(ValueError):
            require_native_segments(['  HELLO!  '], value)
        require_native_segments(['A new benchmark phrase.'], value)


class Playback:
    def __init__(self, emit, send):
        self.results = []
    async def segment(self, result):
        self.results.append(result)
    async def finish(self):
        pass
    async def close(self):
        pass


class PoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.fixture = Fixture(Path(self.directory.name))
        self.rpc = SimpleNamespace(core_id='deskb1x', stopping=False, calls=set(), streams=set(),
                                   client=SimpleNamespace(is_connected=True))
        self.pool = TTSPool(self.rpc, self.fixture.export())

    async def test_normalized_hit_uses_no_nats_or_worker_slot_and_preserves_transcript(self):
        with patch.object(self.pool, 'reserve', side_effect=AssertionError('No worker for cache hit')):
            result = await self.pool.generate(0, '  HELLO!  ')
        self.assertEqual(result.text, 'Recorded greeting')
        self.assertEqual(result.sample_rate, 16000)
        self.assertEqual(result.source, 'soundbank')
        self.assertEqual(self.pool.status()['soundbank_hits'], 1)
        self.assertFalse(self.pool.reserved)
        self.assertFalse(self.rpc.calls)
        self.assertFalse(self.rpc.streams)

    async def test_corrupt_matched_recording_fails_without_synthesis_fallback(self):
        self.fixture.asset.write_bytes(b'corrupt')
        with patch.object(self.pool, 'reserve', side_effect=AssertionError('No silent replacement')):
            with self.assertRaisesRegex(WorkerRpcError, '^tts_invalid_result$'):
                await self.pool.generate(0, 'Hello')
        self.assertFalse(self.rpc.calls)

    async def block_decode(self, disconnect=False):
        entered, release = threading.Event(), threading.Event()
        original = self.pool.soundbank.decode
        def decode(entry):
            entered.set()
            if not release.wait(2):
                raise RuntimeError('Fixture release deadline')
            return original(entry)
        with patch.object(self.pool.soundbank, 'decode', side_effect=decode):
            task = asyncio.create_task(self.pool.generate(0, 'Hello'))
            try:
                await until(entered.is_set)
                if disconnect:
                    for stream in tuple(self.rpc.streams):
                        stream.fail()
                    self.rpc.client.is_connected = True  # Reconnect cannot revive the old guard.
                else:
                    task.cancel()
                    await asyncio.sleep(.02)
                    self.assertFalse(task.done())
                release.set()
                if disconnect:
                    with self.assertRaisesRegex(WorkerRpcError, '^tts_unavailable$'):
                        await task
                else:
                    with self.assertRaises(asyncio.CancelledError):
                        await task
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(self.rpc.calls)
        self.assertFalse(self.rpc.streams)
        self.assertEqual(self.pool.soundbank_hits, 0)

    async def test_cancel_joins_owned_decoder_before_releasing_turn(self):
        await self.block_decode()

    async def test_disconnect_reconnect_discards_late_cached_result(self):
        await self.block_decode(disconnect=True)

    async def test_fast_cache_hit_waits_behind_synthesized_segment_of_same_turn(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def fetch():
            entered.set()
            await release.wait()
            return SegmentResult(0, 'Remote,', bytes(2880), 24000, 'deskb2x', 100)
        # Owned jobs are registered in a set, so use an identity-hashable fixture.
        class Owned:
            worker_id = 'deskb2x'
            async def fetch(self):
                return await fetch()
            async def release(self):
                pass
        async def reserve(*args):
            return Owned()
        turn = TTSTurn(self.pool, None, None, playback_factory=Playback)
        try:
            with patch.object(self.pool, 'reserve', side_effect=reserve):
                turn.feed('Remote, HELLO!')
                finishing = asyncio.create_task(turn.finish())
                await entered.wait()
                await until(lambda: self.pool.soundbank_hits == 1)
                self.assertEqual(turn.playback.results, [])
                release.set()
                await finishing
            self.assertEqual([r.index for r in turn.playback.results], [0, 1])
            self.assertEqual([r.sample_rate for r in turn.playback.results], [24000, 16000])
            self.assertEqual(turn.playback.results[1].text, 'Recorded greeting')
        finally:
            release.set()
            await turn.close()
