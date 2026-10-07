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
        async def generate_stream(revision, dialogue, on_chunk):
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

    async def connect(self, core=None):
        ws=await self.client.ws_connect('/xiaozhi/v1/?from=mqtt_gateway',headers=self.headers())
        await ws.send_json({'type':'hello','version':2,'transport':'websocket','audio_params':AUDIO})
        hello=await ws.receive_json(timeout=1)
        return ws,hello

    async def test_gateway_framing_to_transcript_and_text_response(self):
        ws,hello=await self.connect()
        self.assertEqual(hello['capabilities'],['voice_text'])
        self.assertFalse(hello['conversation_runtime'])
        await ws.send_json({'type':'listen','state':'detect','text':'wake word'})
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
