"""Ordered response-wide resampling/Opus state and paced gateway audio."""
import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor

LOGGER = logging.getLogger('xiaozhi.core.tts')


class PCMEncoder:
    def __init__(self):
        import audioop
        from opuslib_next import Encoder, constants
        self.audioop = audioop
        self.encoder = Encoder(16000, 1, constants.APPLICATION_AUDIO)
        self.encoder.bitrate, self.encoder.complexity = 24000, 10
        self.encoder.signal = constants.SIGNAL_VOICE
        self.buffer, self.resample, self.source_rate = bytearray(), None, None

    def encode(self, pcm, rate, final=False):
        if self.source_rate is not None and rate != self.source_rate:
            raise ValueError('TTS source sample rate changed within turn')
        self.source_rate = rate
        if rate != 16000:
            pcm, self.resample = self.audioop.ratecv(pcm, 2, 1, rate, 16000, self.resample)
        self.buffer.extend(pcm)
        if final and self.buffer and len(self.buffer) % 1920:
            self.buffer.extend(bytes(1920 - len(self.buffer) % 1920))
        packets = []
        while len(self.buffer) >= 1920:
            frame = bytes(self.buffer[:1920]); del self.buffer[:1920]
            packet = self.encoder.encode(frame, 960)
            if not isinstance(packet, bytes) or not 0 < len(packet) <= 1275:
                raise ValueError('Invalid encoded Opus packet')
            packets.append(packet)
        return packets


class TTSPlayback:
    def __init__(self, emit, send_audio, *, encoder_factory=PCMEncoder):
        self.emit, self.send_audio, self.encoder_factory = emit, send_audio, encoder_factory
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='tts-output')
        self.encoder = None
        self.packet_count = 0
        self.next_due = None
        self.closed = self.started = False

    async def packet(self, packet):
        if self.closed:
            raise asyncio.CancelledError
        if self.packet_count >= 5:
            now = time.monotonic()
            if self.next_due is None:
                self.next_due = now
            elif now > self.next_due + .06:
                LOGGER.info('TTS playback underrun; gap_ms=%d', round((now - self.next_due) * 1000))
                self.next_due = now
            await asyncio.sleep(max(0, self.next_due - now))
            self.next_due += .06
        # Existing gateway framing, including sequence and playback timestamp.
        header = bytearray(16)
        header[0] = 1
        header[2:4] = len(packet).to_bytes(2, 'big')
        header[4:8] = self.packet_count.to_bytes(4, 'big')
        header[8:12] = (int(time.time() * 1000) % 2**32).to_bytes(4, 'big')
        header[12:16] = len(packet).to_bytes(4, 'big')
        await asyncio.wait_for(self.send_audio(bytes(header) + packet), 2)
        self.packet_count += 1

    async def segment(self, result):
        if self.closed:
            raise asyncio.CancelledError
        loop = asyncio.get_running_loop()
        if self.encoder is None:
            self.encoder = await loop.run_in_executor(self.executor, self.encoder_factory)
        if not self.started:
            self.started = True
            await self.emit({'type': 'tts', 'state': 'start'})
        await self.emit({'type': 'tts', 'state': 'sentence_start', 'text': result.text})
        for offset in range(0, len(result.pcm), 32768):
            packets = await loop.run_in_executor(self.executor, self.encoder.encode,
                result.pcm[offset:offset + 32768], result.sample_rate)
            for packet in packets:
                await self.packet(packet)
        LOGGER.info('TTS segment sent; index=%d worker_id=%s synth_ms=%d audio_ms=%d',
            result.index, result.worker_id, result.synth_ms, len(result.pcm) * 500 // result.sample_rate)

    async def finish(self):
        if self.encoder:
            packets = await asyncio.get_running_loop().run_in_executor(self.executor,
                self.encoder.encode, b'', self.encoder.source_rate, True)
            for packet in packets:
                await self.packet(packet)
        if self.started:
            # Preserve the existing five-packet prebuffer plus one-frame tail.
            await asyncio.sleep(min(self.packet_count, 6) * .06)
            await self.emit({'type': 'tts', 'state': 'stop'})
            self.started = False

    async def close(self):
        if self.closed:
            return
        self.closed = True
        await asyncio.to_thread(self.executor.shutdown, wait=True, cancel_futures=True)
        self.encoder = None
