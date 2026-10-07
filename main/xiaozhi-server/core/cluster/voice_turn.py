"""A session's voice-to-text coordinator, without any provider imports."""
import asyncio
import logging
import time

from .asr_client import ASRClient
from .asr_protocol import MAX_AUDIO
from .worker_rpc import WorkerRpcError

LOGGER = logging.getLogger('xiaozhi.core.voice')
RECOVERABLE_ASR = {'asr_expired', 'asr_unavailable', 'asr_busy', 'asr_provider_failed'}
RECOVERY_AUDIO_FRAMES = 10


class VoiceTurn:
    def __init__(self, rpc, revision, audio, session_id, send, tts_pool=None, send_audio=None, diagnostics=None):
        self.rpc, self.revision, self.audio = rpc, revision, audio
        self.session_id, self.send = session_id, send
        self.task = self.queue = None
        self.generation = 0
        self.ending = False
        self.tts_pool, self.send_audio = tts_pool, send_audio
        self.current_tts = None
        self.state, self.diagnostics = 'idle', diagnostics
        if tts_pool is not None and (send_audio is None or tts_pool.bundle['revision'] != revision):
            raise ValueError('TTS requires matching voice revision and audio sender')

    def record(self, event, **fields):
        if self.diagnostics is not None:
            self.diagnostics.record(event, self.session_id, generation=self.generation, **fields)

    async def emit(self, value):
        await asyncio.wait_for(self.send({'session_id': self.session_id, **value}), timeout=2)

    async def abort(self):
        previous_generation = self.generation
        self.generation += 1
        tts = self.current_tts
        task, self.task = self.task, None
        self.queue = None
        if task:
            running = not task.done()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if running and self.diagnostics is not None:
                self.diagnostics.record('turn_aborted', self.session_id, generation=previous_generation)
        self.state = 'aborted'
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
        self.state = 'asr'
        self.record('asr_started')
        self.task = asyncio.create_task(self.run(self.generation, self.queue, mode))
        self.task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)

    async def audio_frame(self, data):
        if self.task is None or self.task.done() or self.ending:
            return
        if not 0 < len(data) <= MAX_AUDIO:
            raise ValueError('Invalid Opus frame')
        if self.state == 'asr_restarting':
            # A disconnected worker must not fill the normal input queue and
            # abort the listening task. Keep only bounded recent pre-roll.
            while self.queue.qsize() >= RECOVERY_AUDIO_FRAMES:
                self.queue.get_nowait()
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

    async def transcribe(self, generation, queue, mode, started):
        attempts = 0
        while generation == self.generation:
            if attempts and self.ending:
                return '', None
            async def partial(text):
                if generation == self.generation:
                    await self.emit({'type': 'stt', 'state': 'partial', 'text': text})
            def admitted(worker_id):
                if generation == self.generation:
                    self.state = 'asr'
                    self.record('asr_admitted', worker_id=worker_id,
                                elapsed_ms=round((time.monotonic() - started) * 1000))
            stream = ASRClient(self.rpc.client, self.rpc.core_id, self.revision, self.audio, mode, partial, admitted)
            try:
                text = await stream.transcribe(queue)
                if text.strip() or mode != 'auto' or self.ending:
                    return text, stream
                code = 'asr_empty_result'
            except WorkerRpcError as error:
                if error.code not in RECOVERABLE_ASR or self.ending:
                    raise
                code = error.code
            if generation != self.generation:
                raise asyncio.CancelledError
            self.state = 'asr_restarting'
            self.record('asr_restarting', code=code, elapsed_ms=round((time.monotonic() - started) * 1000))
            LOGGER.warning('ASR reopening while listening; core_id=%s session_id=%s code=%s',
                           self.rpc.core_id, self.session_id, code)
            await self.emit({'type': 'stt', 'state': 'clear'})
            while queue.qsize() > RECOVERY_AUDIO_FRAMES:
                queue.get_nowait()
            # Cancelled by abort/disconnect/new listen/start. No MQTT reconnect,
            # partial transcript promotion or retry of an LLM/TTS stage.
            await asyncio.sleep(min(2, 0.25 * 2 ** min(attempts, 3)))
            attempts += 1
        raise asyncio.CancelledError

    async def run(self, generation, queue, mode):
        tts = None
        failed = False
        started = time.monotonic()
        try:
            text, stream = await self.transcribe(generation, queue, mode, started)
            if generation != self.generation:
                return
            if self.diagnostics is not None and stream is not None:
                self.record('asr_final', worker_id=stream.worker_id,
                            elapsed_ms=round((time.monotonic() - started) * 1000), frames=stream.seq)
            self.ending = True
            await self.emit({'type': 'stt', 'state': 'final', 'text': text})
            if text:
                self.state = 'llm'
                self.record('llm_started')
                first_chunk = True
                if self.tts_pool is not None:
                    from .tts_turn import TTSTurn
                    async def emit_tts(value):
                        if generation != self.generation:
                            raise asyncio.CancelledError
                        if value.get('state') == 'start':
                            self.state = 'tts'
                            self.record('tts_started', elapsed_ms=round((time.monotonic() - started) * 1000))
                        await self.emit(value)
                    async def send_tts(data):
                        if generation != self.generation:
                            raise asyncio.CancelledError
                        await self.send_audio(data)
                    tts = TTSTurn(self.tts_pool, emit_tts, send_tts)
                    self.current_tts = tts
                async def chunk(value, seq):
                    nonlocal first_chunk
                    if generation != self.generation:
                        raise asyncio.CancelledError
                    if first_chunk:
                        first_chunk = False
                        self.record('llm_first_chunk', elapsed_ms=round((time.monotonic() - started) * 1000))
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
                self.record('llm_complete', worker_id=result.get('worker_id'),
                            elapsed_ms=round((time.monotonic() - started) * 1000))
                await self.emit({'type': 'llm', 'state': 'final', 'text': result['text']})
                if tts:
                    await tts.finish()
                    self.record('tts_complete', elapsed_ms=round((time.monotonic() - started) * 1000))
        except asyncio.CancelledError:
            pass
        except WorkerRpcError as error:
            if generation == self.generation:
                failed = True
                self.state = 'error'
                self.record('turn_failed', code=error.code)
                LOGGER.warning('Voice turn failed; core_id=%s session_id=%s code=%s',
                               self.rpc.core_id, self.session_id, error.code)
                await self.emit({'type': 'stt', 'state': 'clear'})
                await self.emit({'type': 'error', 'code': error.code, 'message': 'Voice turn unavailable; please start a new turn'})
        except Exception:
            if generation == self.generation:
                failed = True
                self.state = 'error'
                self.record('turn_failed', code='voice_turn_failed')
                LOGGER.warning('Voice turn failed; core_id=%s session_id=%s code=voice_turn_failed',
                               self.rpc.core_id, self.session_id)
                try:
                    await self.emit({'type': 'stt', 'state': 'clear'})
                    await self.emit({'type': 'error', 'code': 'voice_turn_failed', 'message': 'Voice turn unavailable; please start a new turn'})
                except Exception:
                    pass
        finally:
            if tts:
                await tts.close()
                if generation == self.generation and tts.playback.started and not failed:
                    try:
                        await self.emit({'type': 'tts', 'state': 'stop'})
                    except Exception:
                        pass
                if self.current_tts is tts:
                    self.current_tts = None
            if generation == self.generation:
                try:
                    if failed and tts is not None and tts.playback.started:
                        # Playback failures still need terminal cleanup. An ASR
                        # expiry is recovered inside the existing listening task.
                        await self.emit({'type': 'tts', 'state': 'stop', 'end_conversation': True})
                    await self.emit({'type': 'llm', 'state': 'complete',
                        'text_only': tts is None or tts.playback.packet_count == 0})
                    if not failed:
                        self.state = 'done'
                        self.record('turn_complete', elapsed_ms=round((time.monotonic() - started) * 1000))
                except Exception:
                    pass
