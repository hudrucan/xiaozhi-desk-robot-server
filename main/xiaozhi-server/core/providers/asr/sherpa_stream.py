"""Isolated local streaming transducer; one recognizer, owned per-turn streams."""
import asyncio
import hashlib
import threading
import time
from pathlib import Path


class SherpaEngine:
    def __init__(self, config):
        import sherpa_onnx
        paths = {}
        for key, asset in config['models'].items():
            path = Path(asset['path'])
            if path.is_symlink() or not path.is_file():
                raise ValueError('Required local ASR model is unavailable')
            with path.open('rb') as stream:
                checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
            if checksum != asset['sha256']:
                raise ValueError('Local ASR model checksum differs')
            paths[key] = str(path)
        self.recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(**paths,
            sample_rate=16000, feature_dim=80, num_threads=config['num_threads'],
            provider='cpu', decoding_method='greedy_search', enable_endpoint_detection=False)
        self.lock = threading.Lock()


class SherpaStream:
    def __init__(self, config, partial, engine):
        self.config, self.partial, self.engine = config, partial, engine
        self.stream, self.pending = None, None
        self.last_text, self.last_partial = '', 0

    async def operation(self, function):
        def locked():
            with self.engine.lock:
                return function()
        self.pending = asyncio.create_task(asyncio.to_thread(locked))
        try:
            return await asyncio.shield(self.pending)
        except asyncio.CancelledError:
            # Native inference cannot be interrupted. Join it before stream
            # release so no abandoned thread mutates a later turn.
            await asyncio.gather(self.pending, return_exceptions=True)
            raise
        finally:
            self.pending = None

    async def start(self):
        self.stream = await self.operation(self.engine.recognizer.create_stream)

    def drain(self):
        for _ in range(128):
            if not self.engine.recognizer.is_ready(self.stream):
                return
            self.engine.recognizer.decode_stream(self.stream)
        raise ValueError('ASR decode work exceeds its bound')

    def text(self):
        text = self.engine.recognizer.get_result(self.stream).strip()
        if self.config['sentence_case'] and text:
            text = text.lower(); text = text[0].upper() + text[1:]
        if len(text.encode('utf-8')) > 16384:
            raise ValueError('ASR transcript exceeds limit')
        return text

    async def feed(self, pcm):
        import numpy as np
        def feed():
            samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768
            self.stream.accept_waveform(16000, samples)
            self.drain()
            return self.text()
        text = await self.operation(feed)
        if text and text != self.last_text and time.monotonic() - self.last_partial >= 0.12:
            self.last_text, self.last_partial = text, time.monotonic()
            await self.partial(text)

    async def finish(self):
        import numpy as np
        def finish():
            self.stream.accept_waveform(16000, np.zeros(16 * self.config['final_padding_ms'], dtype=np.float32))
            self.stream.input_finished()
            self.drain()
            return self.text()
        return await self.operation(finish)

    async def close(self):
        if self.pending:
            await asyncio.gather(self.pending, return_exceptions=True)
        self.stream = None
