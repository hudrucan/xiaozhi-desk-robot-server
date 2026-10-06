"""Worker-local Opus decode and Silero endpointing, isolated from app.py."""
import asyncio
import hashlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


class SileroEngine:
    def __init__(self, config):
        import numpy as np
        import onnxruntime as ort
        path = Path(config['model_path'])
        if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError('Invalid VAD model')
        if hashlib.sha256(path.read_bytes()).hexdigest() != config['sha256']:
            raise ValueError('VAD model checksum differs')
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = opts.intra_op_num_threads = 1
        self.session = ort.InferenceSession(str(path), opts, providers=['CPUExecutionProvider'])
        self.np, self.config = np, config


class AudioProcessor:
    def __init__(self, engine):
        import opuslib_next
        self.decoder = opuslib_next.Decoder(16000, 1)
        self.engine = engine
        self.samples, self.buffer, self.silence = 0, bytearray(), 0
        self.voice, self.last = False, False
        self.window = deque(maxlen=5)
        if engine:
            self.state = engine.np.zeros((2, 1, 128), dtype=engine.np.float32)
            self.context = engine.np.zeros((1, 64), dtype=engine.np.float32)

    def decode(self, packet):
        pcm = self.decoder.decode(packet, 960, False)
        if len(pcm) != 1920:
            raise ValueError('Audio frame differs from negotiated 60ms')
        self.samples += len(pcm) // 2
        if self.samples > 16000 * 30:
            raise ValueError('Audio turn exceeds 30 seconds')
        voiced, ended = False, False
        if self.engine:
            np, config = self.engine.np, self.engine.config
            self.buffer.extend(pcm)
            while len(self.buffer) >= 1024:
                data = bytes(self.buffer[:1024]); del self.buffer[:1024]
                samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768
                audio = np.concatenate([self.context, samples.reshape(1, -1)], axis=1)
                output, self.state = self.engine.session.run(None,
                    {'input': audio, 'state': self.state, 'sr': np.array(16000, dtype=np.int64)})
                self.context = audio[:, -64:]
                probability = float(output.item())
                self.last = True if probability >= config['threshold'] else False if probability <= config['threshold_low'] else self.last
                self.window.append(self.last)
                current = self.window.count(True) >= 3
                if current:
                    self.voice, self.silence, voiced = True, 0, True
                elif self.voice:
                    self.silence += 512
            ended = self.voice and not voiced and self.silence * 1000 >= config['min_silence_duration_ms'] * 16000
        return pcm, voiced, ended


class ASRPipeline:
    def __init__(self, bundle, mode, partial, *, engine=None, stream_factory=None):
        self.bundle, self.mode, self.partial = bundle, mode, partial
        self.engine, self.stream_factory = engine, stream_factory
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='asr-audio')
        self.processor = self.stream = None
        self.pre_roll = deque(maxlen=10)

    async def start(self):
        loop = asyncio.get_running_loop()
        self.processor = await loop.run_in_executor(self.executor, AudioProcessor, self.engine if self.mode == 'auto' else None)

    async def feed(self, packet):
        pcm, voice, ended = await asyncio.get_running_loop().run_in_executor(self.executor, self.processor.decode, packet)
        if self.stream is None:
            self.pre_roll.append(pcm)
            if self.mode == 'auto' and not voice:
                return False
            factory = self.stream_factory
            if factory is None:
                from core.providers.asr.gemini_stream import GeminiStream
                factory = GeminiStream
            self.stream = factory(self.bundle['provider'], self.partial)
            await self.stream.start()
            for buffered in self.pre_roll:
                await self.stream.feed(buffered)
            self.pre_roll.clear()
        else:
            await self.stream.feed(pcm)
        return ended

    async def finish(self):
        return await self.stream.finish() if self.stream else ''

    async def close(self):
        try:
            if self.stream:
                await self.stream.close()
        finally:
            # Await owned native inference/decode work before releasing the turn.
            await asyncio.to_thread(self.executor.shutdown, wait=True, cancel_futures=True)
