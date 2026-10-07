"""Loopback-only gateway/core compatibility; no live NATS or provider calls."""
import asyncio
import base64
import hashlib
import hmac
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core.cluster.transport_core import CoreConfig, TransportCore
from test_worker_asr import AUDIO, Bus, Pipeline
from core.cluster.asr_worker import ASRService
from core.cluster.voice_turn import VoiceTurn


class VoiceTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bus = Bus()
        self.worker = ASRService(self.bus,'deskb2x',{'revision':8},Pipeline)
        await self.worker.start()
        self.generated = []
        async def generate_stream(revision, dialogue, on_chunk, **options):
            self.generated.append((revision,dialogue))
            await on_chunk('ans', 0)
            await on_chunk('wer', 1)
            return {'status':'ok','text':'answer'}
        self.rpc = SimpleNamespace(client=self.bus,core_id='deskb1x',generate_stream=generate_stream)
        self.config = CoreConfig('deskb1x','127.0.0.1',8000,frozenset(['127.0.0.1']),'wlan0','/tmp/unused','fixture-key')
        self.core = TransportCore(self.config,self.rpc,8)
        self.app = web.Application()
        self.app.router.add_get('/xiaozhi/v1/',self.core.websocket)
        self.app.router.add_get('/diagnostics',self.core.voice_diagnostics)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.worker.stop()

    def headers(self):
        stamp = str(int(time.time()))
        mac, client = '02:00:00:00:00:01','fixture'
        digest=hmac.new(b'fixture-key',f'{client}|{mac}|{stamp}'.encode(),hashlib.sha256).digest()
        return {'device-id':mac,'client-id':client,'authorization':'Bearer '+base64.urlsafe_b64encode(digest).decode().rstrip('=')+'.'+stamp}

    async def connect(self, core=None, features=None):
        ws=await self.client.ws_connect('/xiaozhi/v1/?from=mqtt_gateway',headers=self.headers())
        await ws.send_json({'type':'hello','version':2,'transport':'websocket','audio_params':AUDIO, 'features':features or {}})
        hello=await ws.receive_json(timeout=1)
        return ws,hello

    async def test_gateway_framing_to_transcript_and_text_response(self):
        ws,hello=await self.connect()
        self.assertEqual(hello['capabilities'],['voice_text'])
        self.assertFalse(hello['conversation_runtime'])
        await ws.send_json({'type':'listen','state':'start','mode':'manual'})
        payload=b'fixture-opus'
        await ws.send_bytes(bytes(12)+len(payload).to_bytes(4,'big')+payload)
        await ws.send_json({'type':'listen','state':'stop'})
        messages=[]
        while True:
            message=await ws.receive_json(timeout=2)
            messages.append(message)
            if message.get('state')=='complete': break
        self.assertEqual(len(self.generated),1)
        self.assertTrue(any(message.get('text')=='transcript' for message in messages))
        self.assertTrue(any(message.get('text')=='answer' for message in messages))
        partials = [message for message in messages if message.get('type') == 'llm' and message.get('state') == 'partial']
        self.assertEqual([(message['seq'], message['text']) for message in partials], [(0, 'ans'), (1, 'wer')])
        self.assertLess(messages.index(partials[-1]), next(index for index, message in enumerate(messages)
            if message.get('type') == 'llm' and message.get('state') == 'final'))
        self.assertFalse(any(message['type']=='tts' for message in messages))
        diagnostics = await (await self.client.get('/diagnostics')).json()
        events = diagnostics['events']
        self.assertIn('asr_admitted', [entry['event'] for entry in events])
        self.assertEqual(next(entry['worker_id'] for entry in events if entry['event'] == 'asr_admitted'), 'deskb2x')
        self.assertNotIn('transcript', str(diagnostics))
        self.assertNotIn('fixture-key', str(diagnostics))
        self.assertEqual(diagnostics['sessions']['done'], 1)
        self.assertEqual((await self.client.get('/diagnostics?secret=value')).status, 403)
        await ws.close()
        await asyncio.sleep(0)
        self.assertFalse(self.worker.turns)

    async def test_device_mcp_is_discovered_and_called_without_blocking_audio_reader(self):
        from test_cluster_mcp import RAW, CALL
        calls = []
        async def generate(revision, dialogue, on_chunk, *, tools, on_tools, seconds):
            self.assertEqual(tools[0]['function']['name'],CALL['name'])
            self.assertEqual(seconds,120)
            calls.extend(await on_tools([CALL]))
            await on_chunk('Device confirmed',0)
            return {'status':'ok','text':'Device confirmed'}
        self.rpc.generate_stream=generate
        ws,hello=await self.connect(features={'mcp':True})
        self.assertEqual(hello['type'],'hello')
        await ws.send_json({'type':'listen','state':'detect','input_mode':'text','text':'Read robot state'})
        methods=[];messages=[]
        while True:
            message=await ws.receive_json(timeout=2)
            messages.append(message)
            if message['type']=='mcp':
                payload=message['payload'];method=payload['method'];methods.append(method)
                if 'id' not in payload:continue
                if method=='initialize':
                    self.assertEqual(payload['params']['capabilities'],{})
                    result={'protocolVersion':'2024-11-05'}
                elif method=='tools/list':result={'tools':[RAW]}
                else:
                    self.assertEqual(payload['params']['name'],RAW['name'])
                    result={'content':[{'type':'text','text':'Fixture device status'}]}
                await ws.send_json({'type':'mcp','payload':{'jsonrpc':'2.0','id':payload['id'],'result':result}})
            if message.get('state')=='complete':break
        self.assertEqual(methods,['initialize','notifications/initialized','tools/list','tools/call'])
        self.assertEqual(calls[0]['result']['content'][0]['text'],'Fixture device status')
        self.assertTrue(any(m.get('text')=='Device confirmed' for m in messages))
        diagnostics=await (await self.client.get('/diagnostics')).json()
        self.assertTrue(diagnostics['mcp'])
        self.assertNotIn('Fixture device status',str(diagnostics))
        self.assertIn('mcp_call_complete',[event['event'] for event in diagnostics['events']])
        await ws.close();await asyncio.sleep(.01)
        self.assertFalse(self.core.mcps)
        self.assertFalse(self.core.voices)

    async def test_legacy_web_chat_trigger_uses_existing_consumption_tool(self):
        from test_cluster_mcp import RAW
        raw={**RAW,'name':'self.web_chat.consume_pending','description':'Consume original message'}
        async def generate(revision, dialogue, on_chunk, *, tools, on_tools, seconds):
            self.assertEqual(dialogue,[{'role':'user','content':'web_chat'}])
            result=await on_tools([{'id':'consume','name':'self_web_chat_consume_pending','arguments':{}}])
            self.assertEqual(result[0]['result']['content'][0]['text'],'Full original typed message')
            await on_chunk('Typed reply',0)
            return {'status':'ok','text':'Typed reply'}
        self.rpc.generate_stream=generate
        ws,_=await self.connect(features={'mcp':True})
        await ws.send_json({'type':'listen','state':'detect','text':'web_chat'})
        while True:
            message=await ws.receive_json(timeout=2)
            if message['type']=='mcp':
                p=message['payload']
                if 'id' not in p:continue
                result=({'protocolVersion':'2024-11-05'} if p['method']=='initialize' else
                        {'tools':[raw]} if p['method']=='tools/list' else
                        {'content':[{'type':'text','text':'Full original typed message'}]})
                await ws.send_json({'type':'mcp','payload':{'jsonrpc':'2.0','id':p['id'],'result':result}})
            if message.get('state')=='complete':break
        await ws.close()

    async def test_transport_only_default_preserves_empty_capabilities(self):
        self.core.voice_revision=None
        ws,hello=await self.connect()
        self.assertEqual(hello['capabilities'],[])
        await ws.send_json({'type':'listen','state':'start','mode':'manual'})
        self.assertEqual((await ws.receive_json(timeout=1))['code'],'conversation_runtime_unavailable')
        self.assertFalse(self.generated)
        await ws.close()

    async def test_abort_does_not_generate_llm_or_leave_asr_turn(self):
        ws,_=await self.connect()
        await ws.send_json({'type':'listen','state':'start','mode':'manual'})
        await ws.receive_json(timeout=1)
        await asyncio.sleep(0.02)
        await ws.send_json({'type':'abort'})
        self.assertEqual((await ws.receive_json(timeout=1))['state'],'clear')
        self.assertFalse(self.generated)
        self.assertFalse(self.worker.turns)
        await ws.close()

    async def test_failed_llm_speaks_error_then_accepts_next_asr_on_same_socket(self):
        from aiohttp import WSMsgType
        from core.cluster.tts_client import SegmentResult
        from core.cluster.tts_turn import TTSTurn
        from core.cluster.worker_rpc import WorkerRpcError
        from test_voice_error_recovery import Playback
        from test_worker_tts_segments import bundle
        async def generate(index, text):
            return SegmentResult(index, text, bytes(3840), 16000, 'deskb2x', 1)
        async def fail(*_, **options):
            raise WorkerRpcError('llm_provider_failed')
        self.rpc.generate_stream = fail
        self.core.tts_pool = SimpleNamespace(bundle=bundle(), generate=generate)
        with patch('core.cluster.tts_turn.TTSTurn', side_effect=
                   lambda pool, emit, send: TTSTurn(pool, emit, send, playback_factory=Playback)):
            ws, _ = await self.connect()
            await ws.send_json({'type': 'listen', 'state': 'detect', 'input_mode': 'text', 'text': 'Fixture'})
            messages, packets = [], []
            while True:
                frame = await ws.receive(timeout=2)
                if frame.type == WSMsgType.BINARY:
                    packets.append(frame.data)
                    continue
                message = frame.json()
                messages.append(message)
                if message.get('type') == 'tts' and message.get('state') == 'stop':
                    # Same transition the firmware makes after spoken playback.
                    await ws.send_json({'type': 'listen', 'state': 'start', 'mode': 'auto'})
                    break
            async with asyncio.timeout(2):
                while not self.worker.turns:
                    await asyncio.sleep(.01)
            self.assertTrue(packets)
            self.assertFalse(any(m.get('end_conversation') or m['type'] == 'error' for m in messages))
            voice = next(iter(self.core.voices.values()))
            self.assertEqual(voice.state, 'asr')
            self.assertFalse(voice.task.done())
            self.assertFalse(ws.closed)
            await ws.close()


    async def test_native_wake_detect_then_listen_keeps_ack_and_reopens_asr(self):
        from aiohttp import WSMsgType
        from core.cluster.tts_client import SegmentResult
        from core.cluster.tts_turn import TTSTurn
        from test_voice_error_recovery import Playback
        from test_worker_tts_segments import bundle
        entered, release = asyncio.Event(), asyncio.Event()
        async def generate(index, text):
            entered.set()
            await release.wait()
            return SegmentResult(index, text, bytes(3840), 16000, 'deskb2x', 1)
        self.core.tts_pool = SimpleNamespace(bundle=bundle(), generate=generate)
        with patch('core.cluster.tts_turn.TTSTurn', side_effect=
                   lambda pool, emit, send: TTSTurn(pool, emit, send, playback_factory=Playback)):
            ws, _ = await self.connect()
            await ws.send_json({'type':'listen', 'state':'detect', 'text':'Hello Xiaozhi'})
            await asyncio.wait_for(entered.wait(), 1)
            await ws.send_json({'type':'listen', 'state':'start', 'mode':'auto'})
            await asyncio.sleep(.02)
            self.assertFalse(self.worker.turns)
            release.set()
            while True:
                frame = await ws.receive(timeout=2)
                if frame.type == WSMsgType.BINARY:
                    continue
                message = frame.json()
                if message.get('type') == 'tts' and message.get('state') == 'stop':
                    self.assertFalse(message.get('end_conversation', False))
                    break
            await ws.send_json({'type':'listen', 'state':'start', 'mode':'auto'})
            async with asyncio.timeout(1):
                while not self.worker.turns:
                    await asyncio.sleep(.01)
            self.assertFalse(self.generated)
            await ws.close()

    async def test_direct_exit_sends_goodbye_without_provider_execution(self):
        ws, _ = await self.connect()
        await ws.send_json({'type':'listen', 'state':'detect', 'input_mode':'text', 'text':'Quit!'})
        messages = []
        while True:
            message = await ws.receive_json(timeout=1)
            messages.append(message)
            if message['type'] == 'goodbye':
                break
        self.assertTrue(any(m.get('end_conversation') is True for m in messages))
        self.assertFalse(self.generated)
        self.assertFalse(self.worker.turns)
        await ws.close()

    async def test_closed_session_is_not_active_while_voice_cleanup_is_pending(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = VoiceTurn.abort
        async def blocked_abort(voice):
            entered.set()
            # Model owned native cleanup that still has to finish after the
            # HTTP transport cancels its handler on peer disconnect.
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass
            await original(voice)
        ws, _ = await self.connect()
        self.assertEqual(len(self.core.sessions), 1)
        with patch.object(VoiceTurn, 'abort', blocked_abort):
            try:
                await ws.close()
                await asyncio.wait_for(entered.wait(), timeout=1)
                self.assertFalse(self.core.sessions)
                self.assertFalse(self.core.voices)
                self.assertEqual(len(self.core.sockets), 1)
            finally:
                release.set()
                async def closed():
                    while self.core.sockets:
                        await asyncio.sleep(0.001)
                await asyncio.wait_for(closed(), timeout=1)


if __name__=='__main__': unittest.main()
