"""Offline LLM failure speech and next-listen recovery; no real providers."""
import asyncio
import copy
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from core.cluster.tts_client import SegmentResult
from core.cluster.tts_config import DEFAULT_ERROR_RESPONSE, validate_bundle
from core.cluster.tts_turn import TTSTurn
from core.cluster.voice_diagnostics import VoiceDiagnostics
from core.cluster.voice_turn import VoiceTurn
from core.cluster.worker_rpc import WorkerRpcError
from core.cluster.llm_worker import safe_error_metadata
from test_worker_tts_segments import bundle


class Playback:
    def __init__(self, emit, send):
        self.emit, self.send = emit, send
        self.started = self.closed = False
        self.packet_count = 0

    async def segment(self, result):
        if not self.started:
            self.started = True
            await self.emit({'type': 'tts', 'state': 'start'})
        await self.emit({'type': 'tts', 'state': 'sentence_start', 'text': result.text})
        await self.send(b'fixture-audio')
        self.packet_count += 1

    async def finish(self):
        if self.started:
            await self.emit({'type': 'tts', 'state': 'stop'})
            self.started = False

    async def close(self):
        self.closed = True


class VoiceFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.messages, self.audio, self.spoken = [], [], []
        self.pool = SimpleNamespace(bundle=bundle(), generate=self.generate)
        self.rpc = SimpleNamespace(core_id='deskb1x', client=None, generate_stream=self.fail_llm)
        self.voice = VoiceTurn(self.rpc, 8, {}, uuid.uuid4().hex, self.emit,
                               self.pool, self.send_audio, VoiceDiagnostics())
        self.patcher = patch('core.cluster.tts_turn.TTSTurn', side_effect=
                             lambda pool, emit, send: TTSTurn(pool, emit, send, playback_factory=Playback))
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    async def asyncTearDown(self):
        await self.voice.abort()

    async def emit(self, value):
        self.messages.append(value)

    async def send_audio(self, value):
        self.audio.append(value)

    async def generate(self, index, text):
        self.spoken.append(text)
        return SegmentResult(index, text, bytes(3840), 16000, 'deskb2x', 1)

    async def fail_llm(self, *_, **options):
        raise WorkerRpcError('llm_provider_failed')

    async def run_failure(self):
        await self.voice.start_text('Fixture request')
        await self.voice.task

    async def test_llm_failure_before_audio_speaks_error_and_allows_next_listen(self):
        await self.run_failure()
        states = [v['state'] for v in self.messages if v['type'] == 'tts']
        self.assertEqual(states[0], 'start')
        self.assertEqual(states[-1], 'stop')
        self.assertIn(DEFAULT_ERROR_RESPONSE, ' '.join(self.spoken))
        self.assertTrue(self.audio)
        self.assertFalse(any(v['type'] == 'error' or v.get('end_conversation') for v in self.messages))
        self.assertFalse(self.messages[-1]['text_only'])
        self.assertIsNone(self.voice.current_tts)
        self.assertIn('turn_failed', [v['event'] for v in self.voice.diagnostics.snapshot()])
        # Firmware's TTS stop -> listen/start opens a fresh ASR task, without
        # reconnecting MQTT or trying the previous model/tool request again.
        async def transcribe(*_):
            await asyncio.Event().wait()
        with patch.object(self.voice, 'transcribe', side_effect=transcribe):
            await self.voice.start('auto')
            await asyncio.sleep(0)
            self.assertEqual(self.voice.state, 'asr')
            self.assertFalse(self.voice.task.done())
            await self.voice.audio_frame(b'next-opus-frame')
            self.assertEqual(self.voice.queue.qsize(), 1)

    async def test_wake_acknowledgement_survives_immediate_listen_start(self):
        entered, release = asyncio.Event(), asyncio.Event()
        generate = self.pool.generate
        async def blocked(index, text):
            entered.set()
            await release.wait()
            return await generate(index, text)
        self.pool.generate = blocked
        await self.voice.start_text('Hello Xiaozhi', wake=True)
        await asyncio.wait_for(entered.wait(), 1)
        task = self.voice.task
        await self.voice.start('auto')
        self.assertIs(self.voice.task, task)
        self.assertFalse(task.done())
        release.set()
        await asyncio.wait_for(task, 1)
        self.assertEqual(self.spoken, ['Hello'])
        self.assertFalse(self.voice.waking)
        self.assertFalse(any(v.get('end_conversation') for v in self.messages))

    async def test_disabled_greeting_keeps_next_listen_available_without_llm(self):
        self.voice.session_config['enable_greeting'] = False
        await self.voice.start_text('Hello Xiaozhi', wake=True)
        self.assertIsNone(self.voice.task)
        self.assertFalse(self.voice.waking)
        self.assertFalse(self.spoken)
        self.assertFalse(any(v['type'] == 'llm' for v in self.messages))

    async def test_direct_exit_ends_without_llm_or_tts_and_exact_matching_only(self):
        await self.voice.start_text('QUIT!')
        await self.voice.task
        self.assertFalse(self.spoken)
        self.assertEqual([v['type'] for v in self.messages][-1], 'goodbye')
        self.assertTrue(any(v.get('end_conversation') for v in self.messages))
        self.messages.clear()
        await self.voice.start_text('Explain the word quit')
        await self.voice.task
        self.assertFalse(any(v['type'] == 'goodbye' for v in self.messages))
        self.assertTrue(self.spoken)

    async def test_exit_tool_speaks_configured_farewell_then_ends(self):
        async def generate(revision, dialogue, on_chunk, **options):
            result = await options['on_tools']([{'id':'exit', 'name':'handle_exit_intent', 'arguments':{}}])
            text = result[0]['result']['text']
            await on_chunk(text, 0)
            return {'status':'ok', 'text':text}
        self.rpc.generate_stream = generate
        self.voice.session_config['exit_farewell'] = 'Fixture farewell'
        await self.voice.start_text('Please end this conversation')
        await self.voice.task
        self.assertEqual(self.spoken, ['Fixture farewell'])
        stop = next(v for v in self.messages if v.get('type') == 'tts' and v.get('state') == 'stop')
        self.assertTrue(stop['end_conversation'])
        self.assertEqual(self.messages[-1]['type'], 'goodbye')
        self.assertLess(self.messages.index(stop), len(self.messages)-1)

    async def test_typed_wake_word_remains_normal_user_input(self):
        await self.voice.start_text('Hello Xiaozhi')
        await self.voice.task
        self.assertIn(DEFAULT_ERROR_RESPONSE, ' '.join(self.spoken))
        self.assertNotEqual(self.spoken, ['Hello'])

    async def test_asr_wake_with_greeting_disabled_keeps_auto_listening(self):
        self.voice.session_config['enable_greeting'] = False
        calls = []
        async def transcribe(*args):
            calls.append(True)
            if len(calls) == 1:
                return 'Hello Xiaozhi', None
            await asyncio.Event().wait()
        with patch.object(self.voice, 'transcribe', side_effect=transcribe):
            await self.voice.start('auto')
            async with asyncio.timeout(1):
                while len(calls) < 2:
                    await asyncio.sleep(0)
            self.assertFalse(self.voice.task.done())
            self.assertEqual(self.voice.state, 'asr')
            self.assertFalse(self.spoken)
            await self.voice.abort()

    async def test_abort_cancels_pending_wake_without_reopening_asr(self):
        entered = asyncio.Event()
        async def blocked(index, text):
            entered.set()
            await asyncio.Event().wait()
        self.pool.generate = blocked
        await self.voice.start_text('Hello Xiaozhi', wake=True)
        await asyncio.wait_for(entered.wait(), 1)
        await self.voice.abort()
        self.assertFalse(self.voice.waking)
        self.assertIsNone(self.voice.task)
        self.assertFalse(self.audio)
        self.assertFalse(any(v['type'] == 'goodbye' for v in self.messages))

    async def test_partial_speech_keeps_one_playback_lifecycle_and_appends_error(self):
        async def fail_after_chunk(revision, dialogue, on_chunk, **options):
            await on_chunk('Partial answer. ', 0)
            async with asyncio.timeout(1):
                while not self.audio:
                    await asyncio.sleep(0)
            raise WorkerRpcError('llm_provider_failed')
        self.rpc.generate_stream = fail_after_chunk
        await self.run_failure()
        states = [v['state'] for v in self.messages if v['type'] == 'tts']
        self.assertEqual(states.count('start'), 1)
        self.assertEqual(states.count('stop'), 1)
        self.assertIn('Partial answer.', self.spoken)
        self.assertIn(DEFAULT_ERROR_RESPONSE, ' '.join(self.spoken))
        self.assertFalse(any(v.get('end_conversation') for v in self.messages))

    async def test_unavailable_error_tts_still_completes_without_recursive_retry(self):
        calls = []
        async def unavailable(index, text):
            calls.append(text)
            raise WorkerRpcError('tts_unavailable')
        self.pool.generate = unavailable
        await self.run_failure()
        self.assertTrue(calls)
        self.assertLessEqual(len(calls), 3)
        self.assertEqual(self.messages[-1]['state'], 'complete')
        self.assertTrue(self.messages[-1]['text_only'])
        self.assertTrue(any(v.get('code') == 'llm_provider_failed' for v in self.messages))
        self.assertIsNone(self.voice.current_tts)

    async def test_abort_cancels_error_speech_without_late_completion(self):
        entered = asyncio.Event()
        async def blocked(index, text):
            entered.set()
            await asyncio.Event().wait()
        self.pool.generate = blocked
        await self.voice.start_text('Fixture request')
        await asyncio.wait_for(entered.wait(), 1)
        await self.voice.abort()
        count = len(self.messages)
        await asyncio.sleep(.01)
        self.assertEqual(len(self.messages), count)
        self.assertFalse(any(v.get('state') == 'complete' for v in self.messages))
        self.assertIsNone(self.voice.current_tts)

    async def test_configured_error_response_and_legacy_bundle_compatibility(self):
        old = bundle()
        self.assertEqual(validate_bundle(copy.deepcopy(old), 'deskb1x'), old)
        custom = {**old, 'system_error_response': 'Configured failure sentence.'}
        validate_bundle(custom, 'deskb1x')
        self.pool.bundle = custom
        self.voice.error_response = custom['system_error_response']
        await self.run_failure()
        self.assertIn(custom['system_error_response'], self.spoken)
        self.assertEqual(custom['fingerprint'], old['fingerprint'])
        for bad in ('', None, 'x' * 2049):
            with self.assertRaises(ValueError):
                validate_bundle({**old, 'system_error_response': bad}, 'deskb1x')


class SafeFailureMetadataTests(unittest.TestCase):
    def test_only_fixed_type_and_numeric_http_status_are_exposed(self):
        class ClientError(Exception):
            code = 400
        error = ClientError('private-provider-url-and-key')
        self.assertEqual(safe_error_metadata(error), ('ClientError', 400))
        error.code = 'private-status'
        self.assertEqual(safe_error_metadata(error), ('ClientError', 0))
        class UntrustedProviderError(Exception):
            code = True
        self.assertEqual(safe_error_metadata(UntrustedProviderError('private')), ('OtherError', 0))


if __name__ == '__main__':
    unittest.main()
