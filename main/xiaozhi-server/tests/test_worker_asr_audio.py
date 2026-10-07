"""Real offline Opus encode/decode and bounded pipeline fixtures; no provider I/O."""
import asyncio
import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.cluster.asr_config import load_bundle
from core.cluster.asr_pipeline import ASRPipeline, AudioProcessor, AudioLimitExceeded
from export_worker_asr_config import build_bundle
from export_worker_llm_config import write_private


class AudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_stream_preserves_order_and_closes(self):
        import opuslib_next
        encoder = opuslib_next.Encoder(16000, 1, opuslib_next.APPLICATION_AUDIO)
        packet = encoder.encode(bytes(1920), 960)
        class Stream:
            def __init__(self, config, partial):
                self.frames, self.closed = [], False
            async def start(self): pass
            async def feed(self, data): self.frames.append(data)
            async def finish(self): return 'fixture'
            async def close(self): self.closed = True
        pipeline = ASRPipeline({'provider': {}}, 'manual', None, stream_factory=Stream)
        try:
            await pipeline.start()
            self.assertFalse(await pipeline.feed(packet))
            self.assertFalse(await pipeline.feed(packet))
            self.assertEqual(await pipeline.finish(), 'fixture')
            self.assertEqual([len(frame) for frame in pipeline.stream.frames], [1920, 1920])
        finally:
            await pipeline.close()
        self.assertTrue(pipeline.stream.closed)

    async def test_auto_silence_never_connects_provider(self):
        import numpy as np
        import opuslib_next
        session = SimpleNamespace(run=lambda *_: (np.array([[0.0]]), np.zeros((2, 1, 128), dtype=np.float32)))
        engine = SimpleNamespace(np=np, session=session,
            config={'threshold': 0.5, 'threshold_low': 0.3, 'min_silence_duration_ms': 200})
        encoder = opuslib_next.Encoder(16000, 1, opuslib_next.APPLICATION_AUDIO)
        packet = encoder.encode(bytes(1920), 960)
        pipeline = ASRPipeline({'provider': {}}, 'auto', None, engine=engine,
            stream_factory=lambda *_: self.fail('Silence must not connect an ASR provider'))
        try:
            await pipeline.start()
            for _ in range(15):
                self.assertFalse(await pipeline.feed(packet))
            self.assertEqual(await pipeline.finish(), '')
            self.assertEqual(len(pipeline.pre_roll), 10)
        finally:
            await pipeline.close()

    async def test_decode_duration_and_total_audio_bounds(self):
        import opuslib_next
        encoder = opuslib_next.Encoder(16000, 1, opuslib_next.APPLICATION_AUDIO)
        processor = AudioProcessor(None)
        with self.assertRaises(ValueError):
            processor.decode(encoder.encode(bytes(640), 320))
        processor = AudioProcessor(None)
        processor.samples = 480000
        with self.assertRaises(AudioLimitExceeded):
            processor.decode(encoder.encode(bytes(1920), 960))

    async def test_vad_silence_is_measured_in_samples_not_elapsed_network_time(self):
        import numpy as np
        import opuslib_next
        probability = [1.0]
        session = SimpleNamespace(run=lambda *_: (np.array([[probability[0]]]), np.zeros((2,1,128), dtype=np.float32)))
        engine = SimpleNamespace(np=np, session=session,
            config={'threshold':0.5,'threshold_low':0.3,'min_silence_duration_ms':200})
        processor = AudioProcessor(engine)
        encoder = opuslib_next.Encoder(16000,1,opuslib_next.APPLICATION_AUDIO)
        packet = encoder.encode(bytes(1920),960)
        for _ in range(3): processor.decode(packet)
        self.assertTrue(processor.voice)
        probability[0] = 0
        results = [processor.decode(packet)[2] for _ in range(8)]
        self.assertFalse(results[0])
        self.assertTrue(results[-1])


class ExportTests(unittest.TestCase):
    def store(self):
        self.resolved = []
        self.config = {'selected_module': {'ASR':'GeminiASR','VAD':'SileroVAD'},
            'ASR': {'GeminiASR': {'type':'gemini','model_name':'fixture-model','api_key':'${secret:ASR}', 'language':'auto','mode':'VERBATIM'}},
            'VAD': {'SileroVAD': {'type':'silero','threshold':0.5,'threshold_low':0.3,'min_silence_duration_ms':200}},
            'LLM': {'unused': {'api_key':'${secret:MISSING}'}}, 'static_soundbank':{'entries':{}}}
        self.snapshot = {'payload': {'manifest': {'revision':8}, 'object': {'schema_version':2,
            'layers':{'global':{},'environments':{},'roles':{},'cluster':{},'nodes':{}}}}}
        def resolve(value):
            self.assertEqual(set(value), {'ASR'})
            self.resolved.append(True)
            result = copy.deepcopy(value); result['ASR']['api_key'] = 'fixture-only'
            return result
        return SimpleNamespace(bootstrap={'node_id':'deskb1x'}, _read_cache=lambda _:self.snapshot,
            _resolve=lambda _: (self.config,{}), secrets=SimpleNamespace(resolve=resolve))

    def test_export_resolves_only_asr_preserves_cloud_and_private_idempotency(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory)/'fixture.onnx'; model.write_bytes(b'offline-fixture')
            checksum = hashlib.sha256(model.read_bytes()).hexdigest()
            store = self.store(); before = copy.deepcopy(self.config)
            bundle = build_bundle(store, model, checksum, 8)
            self.assertEqual(self.config, before)
            self.assertEqual(self.resolved, [True])
            path = Path(directory)/'bundle.json'
            self.assertTrue(write_private(path,bundle))
            self.assertEqual(load_bundle(path,'deskb1x'),bundle)
            self.assertFalse(write_private(path,bundle))
            with self.assertRaises(ValueError): load_bundle(path,'deskb2x')
            with self.assertRaises(ValueError): build_bundle(store,model,'0'*64,8)

    def test_revision_drift_fails_before_resolving_credentials(self):
        store = self.store()
        with self.assertRaises(ValueError): build_bundle(store,'/missing.onnx','0'*64,9)
        self.assertEqual(self.resolved,[])

    def test_selected_local_asr_uses_only_checksums_not_secret_resolution(self):
        store=self.store()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); models=root/'models';models.mkdir()
            for key in ('encoder','decoder','joiner','tokens'):
                (models/key).write_bytes(key.encode())
            self.config['selected_module']['ASR']='Local'
            self.config['ASR']['Local']={'type':'sherpa_streaming','model_dir':'models',
                'encoder':'encoder','decoder':'decoder','joiner':'joiner','tokens':'tokens',
                'num_threads':2,'sentence_case':True,'final_padding_ms':800}
            vad=root/'vad.onnx';vad.write_bytes(b'vad-fixture')
            bundle=build_bundle(store,vad,hashlib.sha256(vad.read_bytes()).hexdigest(),8,root)
            self.assertEqual(bundle['provider']['type'],'sherpa_streaming')
            self.assertEqual(len(bundle['provider']['models']),4)
            self.assertEqual(self.resolved,[])
            (models/'encoder').unlink()
            with self.assertRaises(ValueError):
                build_bundle(store,vad,hashlib.sha256(vad.read_bytes()).hexdigest(),8,root)


if __name__ == '__main__':
    unittest.main()
