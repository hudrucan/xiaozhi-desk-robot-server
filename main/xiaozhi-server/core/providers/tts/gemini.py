import asyncio
import base64
import io
import json
import wave

import aiohttp

from config.logger import setup_logging
from core.providers.tts.base import TTSProviderBase
from core.providers.tts.dto.dto import SentenceType
from core.utils import textUtils
from core.utils.tts import MarkdownCleaner


TAG = __name__
logger = setup_logging()


class TTSProvider(TTSProviderBase):
    SAMPLE_RATE = 24000

    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.api_key = config.get("api_key")
        if not self.api_key or self.api_key in {"your_api_key", "你的api_key"}:
            raise ValueError("Gemini TTS requires a valid API key")

        self.model_name = config.get(
            "model_name", "gemini-3.1-flash-tts-preview"
        )
        self.voice = config.get("voice", "Aoede")
        self.max_retries = max(0, int(config.get("max_retries", 1)))
        self.min_segment_chars = max(1, int(config.get("min_segment_chars", 80)))
        self.audio_file_type = "wav"
        self.punctuations += (".",)
        self.first_sentence_punctuations += (".",)

    async def open_audio_channels(self, conn):
        if conn.sample_rate != self.SAMPLE_RATE:
            raise ValueError(
                "Gemini TTS outputs 24000 Hz PCM, but the client negotiated "
                f"{conn.sample_rate} Hz"
            )
        await super().open_audio_channels(conn)

    def _build_payload(self, text):
        return {
            "model": self.model_name,
            "input": (
                "Generate speech for the transcript below in the language in "
                "which it is written. Read only the transcript exactly as given; "
                "do not add, omit, or translate any words.\n"
                f"TRANSCRIPT:\n{text}"
            ),
            "response_format": {"type": "audio"},
            "generation_config": {
                "speech_config": [{"voice": self.voice}]
            },
            "stream": True,
        }

    async def _stream_pcm(self, text, on_audio):
        timeout = aiohttp.ClientTimeout(total=self.tts_timeout)
        headers = {
            "x-goog-api-key": self.api_key,
            "Api-Revision": "2026-05-20",
            "Accept": "text/event-stream",
        }
        received_audio = False
        event_types = set()
        delta_types = set()

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                "https://generativelanguage.googleapis.com/v1beta/interactions",
                headers=headers,
                json=self._build_payload(text),
            ) as response:
                if response.status != 200:
                    error_body = await response.text()
                    raise RuntimeError(
                        f"Gemini TTS request failed: HTTP {response.status}: "
                        f"{error_body}"
                    )

                async for raw_line in response.content:
                    line = raw_line.decode("utf-8").strip()
                    if not line.startswith("data:"):
                        continue
                    event_data = line[5:].strip()
                    if not event_data or event_data == "[DONE]":
                        continue

                    event = json.loads(event_data)
                    event_type = event.get("event_type", "unknown")
                    event_types.add(event_type)
                    if event_type == "error":
                        error = event.get("error", event)
                        raise RuntimeError(f"Gemini TTS stream error: {error}")
                    if event_type != "step.delta":
                        continue
                    delta = event.get("delta", {})
                    delta_type = delta.get("type", "unknown")
                    delta_types.add(delta_type)
                    if delta_type != "audio" or not delta.get("data"):
                        continue

                    pcm_data = base64.b64decode(delta["data"])
                    if pcm_data:
                        received_audio = True
                        on_audio(pcm_data)

        if not received_audio:
            raise RuntimeError(
                "Gemini TTS returned no audio "
                f"(events={sorted(event_types)}, deltas={sorted(delta_types)})"
            )

    def _get_segment_text(self):
        """Batch short LLM fragments to avoid one Gemini request per sentence."""
        full_text = "".join(self.tts_text_buff)
        current_text = full_text[self.processed_chars :]
        if len(current_text) < self.min_segment_chars:
            return None

        punctuations = (
            self.first_sentence_punctuations
            if self.is_first_sentence
            else self.punctuations
        )
        split_at = max((current_text.rfind(mark) for mark in punctuations), default=-1)
        if split_at < 0:
            return None

        segment_text_raw = current_text[: split_at + 1]
        self.processed_chars += len(segment_text_raw)
        self.is_first_sentence = False
        return textUtils.get_string_no_punctuation_or_emoji(segment_text_raw)

    def to_tts_stream(self, text, opus_handler=None):
        original_text = text
        text = MarkdownCleaner.clean_markdown(text)
        if self._correct_words_pattern:
            text = self._correct_words_pattern.sub(
                lambda match: self.correct_words[match.group(0)], text
            )

        async def generate():
            audio_started = False

            def handle_pcm(pcm_data):
                nonlocal audio_started
                if not audio_started:
                    self.tts_audio_queue.put(
                        (
                            SentenceType.FIRST,
                            None,
                            original_text,
                            getattr(self, "current_sentence_id", None),
                        )
                    )
                    audio_started = True
                self.opus_encoder.encode_pcm_to_opus_stream(
                    pcm_data,
                    end_of_stream=False,
                    callback=opus_handler,
                )

            for attempt in range(self.max_retries + 1):
                try:
                    await self._stream_pcm(text, handle_pcm)
                    self.opus_encoder.encode_pcm_to_opus_stream(
                        b"", end_of_stream=True, callback=opus_handler
                    )
                    logger.bind(tag=TAG).info(
                        f"Gemini TTS generation completed: {original_text}"
                    )
                    return
                except Exception as error:
                    self.opus_encoder.reset_state()
                    if audio_started or attempt >= self.max_retries:
                        raise
                    logger.bind(tag=TAG).warning(
                        f"Gemini TTS attempt {attempt + 1} failed: {error}"
                    )

        try:
            asyncio.run(generate())
        except Exception as error:
            logger.bind(tag=TAG).error(
                f"Gemini TTS generation failed: {original_text}: {error}"
            )

    async def text_to_speak(self, text, output_file):
        pcm_chunks = []
        await self._stream_pcm(text, pcm_chunks.append)
        pcm_data = b"".join(pcm_chunks)

        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(self.SAMPLE_RATE)
            wav_file.writeframes(pcm_data)
        wav_data = wav_buffer.getvalue()

        if output_file:
            with open(output_file, "wb") as audio_file:
                audio_file.write(wav_data)
            return None
        return wav_data
