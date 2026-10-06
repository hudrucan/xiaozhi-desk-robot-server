"""Standalone transcription stream; no server/ConnectionHandler imports."""
import asyncio
import time


class GeminiStream:
    def __init__(self, config, partial):
        from google import genai
        from google.genai import types
        self.types, self.config, self.partial = types, config, partial
        self.client = genai.Client(api_key=config['api_key'])
        self.context = self.session = self.receiver = None
        self.final = asyncio.get_running_loop().create_future()
        self.ending, self.last_partial = False, 0

    async def start(self):
        t = self.types
        config = t.LiveConnectConfig(response_modalities=['TEXT'],
            realtime_input_config=t.RealtimeInputConfig(
                automatic_activity_detection=t.AutomaticActivityDetection(disabled=True)),
            input_audio_transcription=t.AudioTranscriptionConfig(
                language_codes=[] if self.config['language'] == 'auto' else [self.config['language']],
                mode=self.config['mode']))
        self.context = self.client.aio.live.connect(model=self.config['model_name'], config=config)
        self.session = await self.context.__aenter__()
        self.receiver = asyncio.create_task(self.receive())
        await self.session.send_realtime_input(activity_start=t.ActivityStart())

    async def receive(self):
        try:
            while True:
                async for response in self.session.receive():
                    content = response.server_content
                    if content is None:
                        continue
                    interim = getattr(content, 'interim_input_transcription', None)
                    if interim is not None and not self.ending and time.monotonic() - self.last_partial >= 0.12:
                        text = interim.text or ''
                        if len(text.encode('utf-8')) > 16384:
                            raise ValueError('ASR transcript exceeds limit')
                        self.last_partial = time.monotonic()
                        await self.partial(text)
                    final = content.input_transcription
                    if final is not None and self.ending and not self.final.done():
                        text = (final.text or '').strip()
                        if len(text.encode('utf-8')) > 16384:
                            raise ValueError('ASR transcript exceeds limit')
                        self.final.set_result(text)
                        return
                raise RuntimeError('ASR stream closed without final transcript')
        except asyncio.CancelledError:
            pass
        except Exception:
            if not self.final.done():
                self.final.set_exception(RuntimeError('ASR stream unavailable'))

    async def feed(self, pcm):
        if self.final.done():
            self.final.result()
            raise RuntimeError('ASR stream already ended')
        await self.session.send_realtime_input(audio=self.types.Blob(data=pcm, mime_type='audio/pcm;rate=16000'))

    async def finish(self):
        self.ending = True
        await self.session.send_realtime_input(activity_end=self.types.ActivityEnd())
        return await self.final

    async def close(self):
        if self.receiver:
            self.receiver.cancel()
            await asyncio.gather(self.receiver, return_exceptions=True)
        if self.final.done() and not self.final.cancelled():
            self.final.exception()
        else:
            self.final.cancel()
        try:
            if self.context:
                await self.context.__aexit__(None, None, None)
        finally:
            await self.client.aio.aclose()
            self.client.close()
