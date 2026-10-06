"""Isolated ASR provider interfaces with fake engines/SDK connections."""
import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_sherpa_stream_has_owned_partial_final_and_padding(self):
        from core.providers.asr.sherpa_stream import SherpaStream
        class Stream:
            def __init__(self): self.sizes=[];self.finished=False
            def accept_waveform(self, rate, values): self.sizes.append(len(values))
            def input_finished(self): self.finished=True
        native=Stream()
        engine=SimpleNamespace(lock=threading.Lock(),recognizer=SimpleNamespace(
            create_stream=lambda:native,is_ready=lambda _:False,get_result=lambda _:'fixture transcript'))
        partials=[]
        async def partial(text): partials.append(text)
        stream=SherpaStream({'sentence_case':True,'final_padding_ms':800},partial,engine)
        await stream.start()
        await stream.feed(bytes(1920))
        self.assertEqual(partials,['Fixture transcript'])
        self.assertEqual(await stream.finish(),'Fixture transcript')
        self.assertEqual(native.sizes,[960,12800])
        self.assertTrue(native.finished)
        await stream.close()
        self.assertIsNone(stream.stream)
        self.assertIsNone(stream.pending)

    async def test_gemini_activity_boundaries_partial_final_and_close(self):
        from core.providers.asr.gemini_stream import GeminiStream
        queue,events,partials=asyncio.Queue(),[],[]
        class Session:
            async def send_realtime_input(self,**value):
                events.append(next(iter(value)))
                if 'activity_end' in value:
                    await queue.put(SimpleNamespace(server_content=SimpleNamespace(
                        interim_input_transcription=None,input_transcription=SimpleNamespace(text='final transcript'))))
            async def receive(self):
                while True: yield await queue.get()
        class Context:
            closed=False
            async def __aenter__(self): return Session()
            async def __aexit__(self,*_): self.closed=True
        context=Context()
        async def close(): events.append('close')
        fake=SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(connect=lambda **_:context),aclose=close),close=lambda:None)
        async def partial(text): partials.append(text)
        with patch('google.genai.Client',return_value=fake):
            stream=GeminiStream({'api_key':'fixture-only','model_name':'fixture','language':'auto','mode':'VERBATIM'},partial)
            await stream.start()
            await stream.feed(bytes(1920))
            await queue.put(SimpleNamespace(server_content=SimpleNamespace(
                interim_input_transcription=SimpleNamespace(text='partial'),input_transcription=None)))
            await asyncio.sleep(0)
            self.assertEqual(await stream.finish(),'final transcript')
            await stream.close()
        self.assertEqual(partials,['partial'])
        self.assertEqual(events,['activity_start','audio','activity_end','close'])
        self.assertTrue(context.closed)


if __name__=='__main__': unittest.main()
