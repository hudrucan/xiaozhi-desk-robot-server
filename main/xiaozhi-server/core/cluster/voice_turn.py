"""A session's voice-to-text coordinator, without any provider imports."""
import asyncio

from .asr_client import ASRClient
from .asr_protocol import MAX_AUDIO
from .worker_rpc import WorkerRpcError


class VoiceTurn:
    def __init__(self, rpc, revision, audio, session_id, send, tts_pool=None, send_audio=None):
        self.rpc, self.revision, self.audio = rpc, revision, audio
        self.session_id, self.send = session_id, send
        self.task = self.queue = None
        self.generation = 0
        self.ending = False
        self.tts_pool, self.send_audio = tts_pool, send_audio
        self.current_tts = None
        if tts_pool is not None and (send_audio is None or tts_pool.bundle['revision'] != revision):
            raise ValueError('TTS requires matching voice revision and audio sender')

    async def emit(self, value):
        await asyncio.wait_for(self.send({'session_id': self.session_id, **value}), timeout=2)

    async def abort(self):
        self.generation += 1
        tts = self.current_tts
        task, self.task = self.task, None
        self.queue = None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if tts is not None and tts.playback.started:
            try:
                await self.emit({'type': 'tts', 'state': 'stop'})
            except Exception:
                pass

    async def start(self, mode):
        if mode not in ('manual', 'auto'):
            raise ValueError('Unsupported listening mode')
        await self.abort()
        await self.emit({'type': 'stt', 'state': 'clear'})
        self.queue, self.ending = asyncio.Queue(64), False
        self.task = asyncio.create_task(self.run(self.generation, self.queue, mode))
        self.task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)

    async def audio_frame(self, data):
        if self.task is None or self.task.done() or self.ending:
            return
        if not 0 < len(data) <= MAX_AUDIO:
            raise ValueError('Invalid Opus frame')
        try:
            self.queue.put_nowait(('audio', data))
        except asyncio.QueueFull:
            await self.abort()
            await self.emit({'type': 'error', 'code': 'asr_overflow', 'message': 'Voice input exceeded its bounded queue'})

    async def stop(self):
        if self.task and not self.task.done() and not self.ending:
            self.ending = True
            await self.emit({'type': 'stt', 'state': 'clear'})
            try:
                self.queue.put_nowait(('end', b''))
            except asyncio.QueueFull:
                await self.abort()
                await self.emit({'type': 'error', 'code': 'asr_overflow', 'message': 'Voice input exceeded its bounded queue'})

    async def run(self, generation, queue, mode):
        tts = None
        try:
            async def partial(text):
                if generation == self.generation:
                    await self.emit({'type': 'stt', 'state': 'partial', 'text': text})
            stream = ASRClient(self.rpc.client, self.rpc.core_id, self.revision, self.audio, mode, partial)
            text = await stream.transcribe(queue)
            if generation != self.generation:
                return
            self.ending = True
            await self.emit({'type': 'stt', 'state': 'final', 'text': text})
            if text:
                if self.tts_pool is not None:
                    from .tts_turn import TTSTurn
                    async def emit_tts(value):
                        if generation != self.generation:
                            raise asyncio.CancelledError
                        await self.emit(value)
                    async def send_tts(data):
                        if generation != self.generation:
                            raise asyncio.CancelledError
                        await self.send_audio(data)
                    tts = TTSTurn(self.tts_pool, emit_tts, send_tts)
                    self.current_tts = tts
                async def chunk(value, seq):
                    if generation != self.generation:
                        raise asyncio.CancelledError
                    if tts:
                        tts.feed(value)
                    # Additive progress event; existing firmware still handles
                    # the unchanged full final text and text-only completion.
                    await self.emit({'type': 'llm', 'state': 'partial', 'seq': seq, 'text': value})
                # The same immutable revision is required by both worker stages.
                call = self.rpc.generate_stream(self.revision, [{'role': 'user', 'content': text}], chunk)
                result = await tts.wait_llm(call) if tts else await call
                if result['status'] != 'ok':
                    raise WorkerRpcError(result['error'])
                if generation != self.generation:
                    return
                await self.emit({'type': 'llm', 'state': 'final', 'text': result['text']})
                if tts:
                    await tts.finish()
        except asyncio.CancelledError:
            pass
        except WorkerRpcError as error:
            if generation == self.generation:
                await self.emit({'type': 'stt', 'state': 'clear'})
                await self.emit({'type': 'error', 'code': error.code, 'message': 'Voice turn unavailable; please start a new turn'})
        except Exception:
            if generation == self.generation:
                try:
                    await self.emit({'type': 'stt', 'state': 'clear'})
                    await self.emit({'type': 'error', 'code': 'voice_turn_failed', 'message': 'Voice turn unavailable; please start a new turn'})
                except Exception:
                    pass
        finally:
            if tts:
                await tts.close()
                if generation == self.generation and tts.playback.started:
                    try:
                        await self.emit({'type': 'tts', 'state': 'stop'})
                    except Exception:
                        pass
                if self.current_tts is tts:
                    self.current_tts = None
            if generation == self.generation:
                try:
                    await self.emit({'type': 'llm', 'state': 'complete',
                        'text_only': tts is None or tts.playback.packet_count == 0})
                except Exception:
                    pass
