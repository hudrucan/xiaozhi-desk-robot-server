"""Offline inactivity ownership and legacy bundle compatibility fixtures."""
import asyncio
import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from core.cluster import asr_protocol as wire
from core.cluster import voice_turn as voice_module
from core.cluster.asr_client import ASRClient
from core.cluster.session_config import DEFAULTS, LEGACY_FIELDS, export_config, validate
from core.cluster.voice_turn import VoiceTurn


class IdleTests(unittest.IsolatedAsyncioTestCase):
    def voice(self):
        messages=[]
        async def send(value):
            messages.append(value)
        voice=VoiceTurn(SimpleNamespace(core_id='deskb1x'),11,{},'fixture',send)
        return voice,messages

    def test_legacy_session_and_optional_closing_fields_remain_usable(self):
        value={key:copy.deepcopy(DEFAULTS[key]) for key in LEGACY_FIELDS}
        self.assertEqual(validate(value),value)
        self.assertEqual(export_config({})['close_connection_no_voice_time'],120)
        self.assertFalse(export_config({'end_prompt':{'enable':False}})['end_prompt']['enable'])
        for seconds in (True,0,-1,3601,'120'):
            with self.assertRaises(ValueError):
                validate({**value,'close_connection_no_voice_time':seconds})

    async def test_silent_audio_does_not_reset_idle_timer(self):
        voice,_=self.voice()
        voice.last_activity=10
        voice.queue=asyncio.Queue(64)
        voice.task=asyncio.create_task(asyncio.Event().wait())
        await voice.audio_frame(b'opus-silence')
        self.assertEqual(voice.last_activity,10)
        self.assertFalse(voice.idle_due(129.9))
        self.assertTrue(voice.idle_due(130))
        await voice.abort()

    async def test_busy_compute_is_protected_and_idle_expiry_is_single_owner(self):
        voice,_=self.voice()
        with patch.object(voice_module,'time',SimpleNamespace(monotonic=lambda:200)):
            for state in ('llm','tts'):
                voice.state=state
                voice.last_activity=1
                self.assertFalse(voice.idle_due(200))
                self.assertEqual(voice.last_activity,200)
            voice.state='done'
            self.assertFalse(voice.idle_due(319))
            self.assertTrue(voice.idle_due(320))
            voice.idle_closing=True
            self.assertFalse(voice.idle_due(1000))

    async def test_expiry_cancels_listening_and_sends_terminal_firmware_contract(self):
        voice,messages=self.voice()
        voice.task=asyncio.create_task(asyncio.Event().wait())
        task=voice.task
        await voice.expire_idle()
        self.assertTrue(task.cancelled())
        self.assertTrue(voice.idle_closing)
        self.assertEqual(messages[-2],{'session_id':'fixture','type':'tts','state':'stop','end_conversation':True})
        self.assertEqual(messages[-1],{'session_id':'fixture','type':'goodbye','reason':'no_voice_timeout'})

    async def test_closing_prompt_uses_configured_text_without_user_transcript(self):
        voice,_=self.voice()
        voice.tts_pool=SimpleNamespace()
        voice.session_config['end_prompt']={'enable':True,'prompt':'Fixture closing instruction'}
        voice.run=AsyncMock()
        await voice.expire_idle()
        voice.run.assert_awaited_once_with(voice.generation,None,None,text='Fixture closing instruction',closing=True)
        self.assertTrue(voice.close_after_chat)

    async def test_configured_disabled_closing_skips_model_request(self):
        voice,messages=self.voice()
        voice.tts_pool=SimpleNamespace()
        voice.session_config['end_prompt']={'enable':False}
        voice.run=AsyncMock()
        await voice.expire_idle()
        voice.run.assert_not_called()
        self.assertEqual(messages[-1]['type'],'goodbye')

    async def test_speech_event_is_owned_and_stale_or_malformed_events_do_not_touch(self):
        observed=[]
        async def partial(text):
            pass
        client=ASRClient(SimpleNamespace(),'deskb1x',11,{},'auto',partial,on_speech=lambda:observed.append(True))
        client.worker_id='deskb2x'
        client.token='b'*32
        data=wire.event(client.turn_id,'deskb2x',11,client.token,'speech')
        await client.receive(SimpleNamespace(data=data))
        self.assertEqual(observed,[True])
        for invalid in (wire.event(client.turn_id,'deskb3x',11,client.token,'speech'),
                        wire.event(client.turn_id,'deskb2x',11,'c'*32,'speech'),
                        wire.event(client.turn_id,'deskb2x',11,client.token,'speech',text='unexpected')):
            await client.receive(SimpleNamespace(data=invalid))
        self.assertEqual(observed,[True])
        client.final.set_result('done')
        await client.receive(SimpleNamespace(data=data))
        self.assertEqual(observed,[True])


if __name__=='__main__':
    unittest.main()
