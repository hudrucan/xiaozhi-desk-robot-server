"""A session's voice-to-text coordinator, without any provider imports."""
import asyncio
import logging
import time
import copy

from core.utils.wakeup_match import matches_wakeup_word, remove_punctuation_and_length
from plugins_func.tool_schemas import handle_exit_intent_function_desc
from .session_config import DEFAULTS as SESSION_DEFAULTS

from .asr_client import ASRClient
from .asr_protocol import MAX_AUDIO
from .worker_rpc import WorkerRpcError

LOGGER = logging.getLogger('xiaozhi.core.voice')
RECOVERABLE_ASR = {'asr_expired', 'asr_unavailable', 'asr_busy', 'asr_provider_failed'}
RECOVERY_AUDIO_FRAMES = 10


class VoiceTurn:
    def __init__(self, rpc, revision, audio, session_id, send, tts_pool=None, send_audio=None, diagnostics=None, mcp=None, *, device_id=None):
        self.rpc, self.revision, self.audio = rpc, revision, audio
        self.session_id, self.send = session_id, send
        self.task = self.queue = None
        self.generation = 0
        self.ending = False
        self.tts_pool, self.send_audio = tts_pool, send_audio
        self.current_tts = None
        self.state, self.diagnostics = 'idle', diagnostics
        self.mcp = mcp
        from .memory_client import MemoryClient, SessionHistory
        self.history = SessionHistory()
        self.memory = None
        if tts_pool is not None and tts_pool.bundle.get('memory') is not None and device_id is not None:
            self.memory = MemoryClient(rpc, tts_pool.bundle['memory'], device_id, tts_pool.bundle['workers'])
        self.session_config = copy.deepcopy(tts_pool.bundle.get('session', SESSION_DEFAULTS)
                                            if tts_pool is not None else SESSION_DEFAULTS)
        self.close_after_chat = False
        self.waking = False
        from .tts_config import DEFAULT_ERROR_RESPONSE
        self.error_response = (tts_pool.bundle.get('system_error_response', DEFAULT_ERROR_RESPONSE)
                               if tts_pool is not None else DEFAULT_ERROR_RESPONSE)
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
        if self.mcp is not None:
            await self.mcp.cancel_vision()
        self.waking = False
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
        if self.waking and self.task is not None and not self.task.done():
            # Firmware sends detect followed immediately by listen/start.
            # The acknowledgement owns playback first; its stop permits the
            # subsequent listen/start to admit ASR normally.
            return
        await self.abort()
        self.close_after_chat = False
        await self.emit({'type': 'stt', 'state': 'clear'})
        self.queue, self.ending = asyncio.Queue(64), False
        self.state = 'asr'
        self.record('asr_started')
        self.task = asyncio.create_task(self.run(self.generation, self.queue, mode))
        self.task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)

    async def start_text(self, text, *, wake=False):
        if not isinstance(text, str) or not text.strip() or len(text) > 512:
            raise ValueError('Invalid typed text')
        await self.abort()
        self.close_after_chat = False
        self.waking = wake
        if (wake and not self.session_config['enable_greeting']
                and matches_wakeup_word(text, self.session_config['wakeup_words'])):
            self.waking = False
            await self.emit({'type':'stt', 'state':'final', 'text':text})
            await self.emit({'type':'tts', 'state':'stop'})
            return
        self.state, self.ending = 'llm', True
        self.task = asyncio.create_task(self.run(self.generation, None, None, text=text, wake=wake))
        self.task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)

    async def tools(self):
        inventory = []
        if self.mcp is not None:
            try:
                inventory = await self.mcp.tools()
            except (WorkerRpcError, asyncio.TimeoutError):
                pass  # Server functions remain usable without a device inventory.
        if self.memory is not None:
            inventory.extend(self.memory.tools())
        inventory.append(copy.deepcopy(handle_exit_intent_function_desc))
        return inventory

    async def execute_tools(self, calls):
        from . import tool_stream_protocol as wire
        wire.calls(calls)
        results = []
        for call in calls:
            if self.close_after_chat:
                result = {'isError':True, 'error':'Conversation is ending; tool was not executed'}
            elif call['name'] == 'handle_exit_intent':
                if call['arguments']:
                    result = {'isError':True, 'error':'Exit tool accepts no arguments'}
                else:
                    self.close_after_chat = True
                    result = {'isError':False, 'action':'end_conversation',
                              'text':self.session_config['exit_farewell']}
            elif call['name'] == 'manage_memory' and self.memory is not None:
                results.append(await self.memory.execute(call, self.history))
                continue
            elif self.mcp is not None:
                results.extend(await self.mcp.execute([call]))
                continue
            else:
                result = {'isError':True, 'error':'Tool is not available in this session'}
            results.append({'id':call['id'], 'result':result})
        return results

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

    async def run(self, generation, queue, mode, text=None, wake=False):
        tts = None
        failed = False
        llm_running = False
        failure_spoken = False
        started = time.monotonic()
        try:
            stream = None
            if text is None:
                while True:
                    text, stream = await self.transcribe(generation, queue, mode, started)
                    if (generation == self.generation and mode == 'auto'
                            and self.session_config['enable_wakeup_words_response_cache']
                            and not self.session_config['enable_greeting']
                            and matches_wakeup_word(text, self.session_config['wakeup_words'])):
                        await self.emit({'type':'stt', 'state':'clear'})
                        continue
                    break
            if generation != self.generation:
                return
            if self.diagnostics is not None and stream is not None:
                self.record('asr_final', worker_id=stream.worker_id,
                            elapsed_ms=round((time.monotonic() - started) * 1000), frames=stream.seq)
            self.ending = True
            await self.emit({'type': 'stt', 'state': 'final', 'text': text})
            if text:
                fixed_response = None
                normalized = remove_punctuation_and_length(text)[1].casefold()
                if normalized and any(normalized == remove_punctuation_and_length(cmd)[1].casefold()
                                      for cmd in self.session_config['exit_commands']):
                    # Explicit exit commands end immediately, as in app.py.
                    self.close_after_chat = True
                    return
                if (wake or (queue is not None and self.session_config['enable_wakeup_words_response_cache'])):
                    if matches_wakeup_word(text, self.session_config['wakeup_words']):
                        fixed_response = self.session_config['wakeup_greeting'] if self.session_config['enable_greeting'] else ''
                if fixed_response == '':
                    self.waking = False
                    await self.emit({'type':'tts', 'state':'stop'})
                    return
                self.state = 'llm'
                llm_running = fixed_response is None
                if llm_running:
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
                        if value.get('state') == 'stop' and self.close_after_chat:
                            value = {**value, 'end_conversation':True}
                        if value.get('state') == 'stop':
                            self.waking = False
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
                if fixed_response is not None:
                    await chunk(fixed_response, 0)
                    result = {'status':'ok', 'text':fixed_response}
                else:
                    tools = await self.tools()
                    if generation != self.generation:
                        raise asyncio.CancelledError
                    options = {'tools':tools, 'on_tools':self.execute_tools, 'seconds':120}
                    if self.memory is not None:
                        options['memory_context'] = await self.memory.recall(text, self.history)
                        if generation != self.generation:
                            raise asyncio.CancelledError
                    call = self.rpc.generate_stream(self.revision, self.history.dialogue(text), chunk, **options)
                    result = await tts.wait_llm(call) if tts else await call
                if result['status'] != 'ok':
                    raise WorkerRpcError(result['error'])
                llm_running = False
                if generation != self.generation:
                    return
                if fixed_response is None:
                    self.record('llm_complete', worker_id=result.get('worker_id'),
                                elapsed_ms=round((time.monotonic() - started) * 1000))
                await self.emit({'type': 'llm', 'state': 'final', 'text': result['text']})
                if tts:
                    await tts.finish()
                    self.record('tts_complete', elapsed_ms=round((time.monotonic() - started) * 1000))
                if fixed_response is None and generation == self.generation:
                    self.history.complete(text, result['text'])
        except asyncio.CancelledError:
            pass
        except WorkerRpcError as error:
            if generation == self.generation:
                failed = True
                self.state = 'error'
                self.record('turn_failed', code=error.code)
                LOGGER.warning('Voice turn failed; core_id=%s session_id=%s code=%s',
                               self.rpc.core_id, self.session_id, error.code)
                failure_spoken = await self.report_failure(generation, error.code, tts, llm_running, started)
        except Exception:
            if generation == self.generation:
                failed = True
                self.state = 'error'
                self.record('turn_failed', code='voice_turn_failed')
                LOGGER.warning('Voice turn failed; core_id=%s session_id=%s code=voice_turn_failed',
                               self.rpc.core_id, self.session_id)
                try:
                    failure_spoken = await self.report_failure(generation, 'voice_turn_failed', tts, llm_running, started)
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
                    self.waking = False
                    if self.close_after_chat and (tts is None or not tts.playback.packet_count):
                        await self.emit({'type':'tts', 'state':'stop', 'end_conversation':True})
                    if failed and not failure_spoken and tts is not None and tts.playback.started:
                        # Playback failures still need terminal cleanup. An ASR
                        # expiry is recovered inside the existing listening task.
                        await self.emit({'type': 'tts', 'state': 'stop', 'end_conversation': True})
                    await self.emit({'type': 'llm', 'state': 'complete',
                        'text_only': tts is None or tts.playback.packet_count == 0})
                    if not failed:
                        self.state = 'done'
                        self.record('turn_complete', elapsed_ms=round((time.monotonic() - started) * 1000))
                    if self.close_after_chat:
                        await self.emit({'type':'goodbye', 'reason':'exit_intent'})
                except Exception:
                    pass

    async def report_failure(self, generation, code, tts, llm_running, started):
        """Speak a fixed configured failure without retrying the LLM or device calls.

        Normal TTS stop lets firmware re-enter listening and request its next
        ASR turn. A protocol error alone does not have that lifecycle.
        """
        await self.emit({'type': 'stt', 'state': 'clear'})
        if llm_running and tts is not None and not tts.failure.done():
            try:
                if generation != self.generation:
                    raise asyncio.CancelledError
                # Reuse the response's ordered stream, including any partial
                # speech already queued, as the normal app.py failure path does.
                tts.feed('\n' + self.error_response)
                await self.emit({'type': 'llm', 'state': 'final', 'text': self.error_response})
                await tts.finish()
                if generation != self.generation:
                    raise asyncio.CancelledError
                self.state = 'error'
                self.record('tts_complete', elapsed_ms=round((time.monotonic() - started) * 1000))
                return True
            except Exception:
                LOGGER.warning('Voice error response unavailable; core_id=%s session_id=%s',
                               self.rpc.core_id, self.session_id)
        if generation == self.generation:
            await self.emit({'type': 'error', 'code': code,
                             'message': 'Voice turn unavailable; please start a new turn'})
        return False
