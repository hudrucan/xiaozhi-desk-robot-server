"""Offline emotion capability, stream ownership and existing firmware contract."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.cluster.voice_turn import VoiceTurn
from core.cluster.llm_worker import LLMWorker
from core.cluster.llm_stream_client import LLMStreamClient
from core.cluster.nats_config import NatsConfig, NatsConnectionConfig
from core.cluster.worker_rpc import WorkerRPC
from core.cluster import tool_stream_protocol as wire
from core.utils.text_utils import EMOTION_BY_EMOJI, extract_emotion
from test_worker_llm_stream import Bus


class EmotionTurnTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.messages, self.voices, self.options = [], [], []
        self.parts = ['😔', ' A sad reply.', '🙂']
        async def generate(revision, dialogue, chunk, **options):
            self.options.append(options)
            for seq, value in enumerate(self.parts):
                await chunk(value, seq)
            return {'status':'ok', 'text':''.join(self.parts)}
        self.rpc = SimpleNamespace(core_id='deskb1x', generate_stream=generate)

    async def asyncTearDown(self):
        for voice in self.voices:
            await voice.abort()

    def voice(self, **options):
        async def send(value):
            self.messages.append(value)
        voice = VoiceTurn(self.rpc, 11, {}, 'fixture-session', send, **options)
        self.voices.append(voice)
        return voice

    def emotions(self):
        return [value for value in self.messages if 'emotion' in value]

    async def reply(self, voice):
        await voice.start_text('Fixture request')
        await voice.task

    async def test_one_emotion_before_text_preserves_full_response(self):
        await self.reply(self.voice())
        self.assertEqual(self.emotions(), [{'type':'llm', 'text':'😔', 'emotion':'sad',
                                           'session_id':'fixture-session'}])
        emotion_index = self.messages.index(self.emotions()[0])
        partial_index = next(i for i, value in enumerate(self.messages) if value.get('state')=='partial')
        self.assertLess(emotion_index, partial_index)
        final = next(value for value in self.messages if value.get('state')=='final' and value['type']=='llm')
        self.assertEqual(final['text'], ''.join(self.parts))
        self.assertTrue(self.options[0]['emoji_enabled'])

    async def test_whitespace_waits_and_missing_marker_uses_existing_default(self):
        self.parts = [' \n', 'Plain reply', '😠']
        await self.reply(self.voice())
        self.assertEqual([(v['text'],v['emotion']) for v in self.emotions()], [extract_emotion('Plain reply')])
        self.assertEqual(self.messages[self.messages.index(self.emotions()[0])-1]['text'], ' \n')

    async def test_disabled_capability_suppresses_emotion_and_reaches_worker(self):
        await self.reply(self.voice(emoji_enabled=False))
        self.assertEqual(self.emotions(), [])
        self.assertFalse(self.options[0]['emoji_enabled'])

    async def test_each_new_turn_owns_a_new_emotion(self):
        voice = self.voice()
        await self.reply(voice)
        self.parts = ['😆 A cheerful reply.']
        await self.reply(voice)
        self.assertEqual([v['emotion'] for v in self.emotions()], ['sad','laughing'])

    async def test_abort_rejects_late_chunk_before_sending_emotion(self):
        entered = asyncio.Event()
        callback = None
        async def blocked(revision, dialogue, chunk, **options):
            nonlocal callback
            callback = chunk
            entered.set()
            await asyncio.Event().wait()
        self.rpc.generate_stream = blocked
        voice = self.voice()
        await voice.start_text('Fixture request')
        await entered.wait()
        await voice.abort()
        with self.assertRaises(asyncio.CancelledError):
            await callback('😠 Late text',0)
        self.assertFalse(self.emotions())

    async def test_emotion_precedes_tts_feed_without_changing_speech_text(self):
        events = []
        class Turn:
            def __init__(self, pool, emit, send):
                self.playback = SimpleNamespace(started=False, packet_count=1)
            def feed(self, text):
                events.append(('feed',text))
            async def wait_llm(self, call):
                return await call
            async def finish(self):
                pass
            async def close(self):
                pass
        voice = self.voice(tts_pool=SimpleNamespace(bundle={'revision':11}), send_audio=lambda _:None)
        async def send(value):
            self.messages.append(value)
            if 'emotion' in value:
                events.append(('emotion',value['emotion']))
        voice.send = send
        with patch('core.cluster.tts_turn.TTSTurn',Turn):
            await self.reply(voice)
        self.assertEqual(events[0],('emotion','sad'))
        self.assertEqual([value for kind,value in events if kind=='feed'],self.parts)

    async def test_wake_ack_has_no_llm_emotion(self):
        voice = self.voice()
        await voice.start_text('Hello Xiaozhi',wake=True)
        await voice.task
        self.assertFalse(self.emotions())
        self.assertFalse(self.options)

    def test_supported_emotions_reuse_app_mapping(self):
        for marker, emotion in EMOTION_BY_EMOJI.items():
            self.assertEqual(extract_emotion(marker+' Reply.'),(marker,emotion))


class EmotionPromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_capability_policy_through_nats_and_legacy_request_preserved(self):
        bus, prompts = Bus(), []
        class Provider:
            async def response_tools_async(self, messages, tools):
                prompts.append(messages[0]['content'])
                yield '🙂 Fixture reply.'
        config = NatsConfig(('nats://fixture:4222',),'fixture','fixture','deskb2x')
        worker = LLMWorker(config,asyncio.Event(),{'revision':11,'prompt':'Configured prompt'},Provider())
        worker.client = bus
        await bus.subscribe(wire.SUBJECT,cb=worker._receive_tools,queue=wire.QUEUE_GROUP)
        rpc = WorkerRPC(NatsConnectionConfig(config.servers,config.username,config.password),
                        'deskb1x',client_factory=lambda:bus)
        async def chunk(*_):
            pass
        async def execute(_):
            self.fail('No tools expected')
        try:
            for options in ({'emoji_enabled':True},{'emoji_enabled':False},{}):
                await rpc.generate_stream(11,[{'role':'user','content':'Fixture'}],chunk,
                    tools=[],on_tools=execute,**options)
            self.assertIn('Use at most one emoji, only at the beginning',prompts[0])
            self.assertTrue(all(marker in prompts[0] for marker in EMOTION_BY_EMOJI))
            self.assertIn('Do not use emoji.',prompts[1])
            self.assertEqual(prompts[2],'Configured prompt')
            requests = [json.loads(data) for subject,data in bus.published if subject==wire.SUBJECT]
            self.assertEqual([value.get('emoji_enabled') for value in requests],[True,False,None])
            for invalid in ('true',1,None):
                with self.assertRaises(ValueError):
                    wire.request(wire.rpc.encode({**requests[0],'emoji_enabled':invalid},wire.MAX_REQUEST_BYTES))
        finally:
            await rpc.close()
            await worker._shutdown()

    async def test_plain_text_protocol_does_not_accept_emotion_option(self):
        with self.assertRaises(ValueError):
            LLMStreamClient(Bus(),'deskb1x',11,[{'role':'user','content':'Fixture'}],30,
                            lambda *_:None,emoji_enabled=True)
