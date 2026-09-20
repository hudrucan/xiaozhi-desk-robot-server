import base64
import json
import os
from typing import List, Optional, Tuple

import aiohttp

from config.logger import setup_logging
from core.providers.asr.base import ASRProviderBase
from core.providers.asr.dto.dto import InterfaceType


TAG = __name__
logger = setup_logging()


class ASRProvider(ASRProviderBase):
    def __init__(self, config: dict, delete_audio_file: bool):
        super().__init__()
        self.interface_type = InterfaceType.NON_STREAM
        self.api_key = config.get("api_key")
        if not self.api_key or self.api_key in {"your_api_key", "你的api_key"}:
            raise ValueError("Gemini ASR requires a valid API key")

        self.model_name = config.get("model_name", "gemini-3.5-flash-lite")
        self.language = config.get("language", "auto")
        self.mode = config.get("mode", "verbatim")
        self.timeout = int(config.get("timeout", 120))
        self.output_dir = config.get("output_dir", "tmp/")
        self.delete_audio_file = delete_audio_file
        os.makedirs(self.output_dir, exist_ok=True)
        self._session = None

    async def speech_to_text(
        self,
        opus_data: List[bytes],
        session_id: str,
        artifacts: Optional[ASRProviderBase.AudioArtifacts] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        if artifacts is None or not artifacts.pcm_bytes:
            return "", None

        wav_data = self._pcm_to_wav(artifacts.pcm_bytes)
        if not wav_data:
            return "", artifacts.file_path

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )

        audio_input = {
            "type": "audio",
            "data": base64.b64encode(wav_data).decode("ascii"),
            "mime_type": "audio/wav",
        }
        if "transcribe" in self.model_name:
            payload = {
                "model": self.model_name,
                "input": [audio_input],
                "generation_config": {
                    "transcription_config": {
                        "language_codes": (
                            [] if self.language == "auto" else [self.language]
                        ),
                        "mode": self.mode,
                    }
                },
            }
        else:
            language_instruction = (
                "Detect the spoken language automatically"
                if self.language == "auto"
                else f"Transcribe the speech in {self.language}"
            )
            prompt = (
                f"{language_instruction}. Return only the transcript, without "
                "labels, formatting, or explanation."
            )
            if self.mode.lower() == "verbatim":
                prompt += " Preserve the spoken wording and disfluencies verbatim."
            payload = {
                "model": self.model_name,
                "input": [
                    {"type": "text", "text": prompt},
                    audio_input,
                ],
            }
        async with self._session.post(
            "https://generativelanguage.googleapis.com/v1beta/interactions",
            headers={"x-goog-api-key": self.api_key},
            json=payload,
        ) as response:
            response_text = await response.text()
            try:
                response_data = json.loads(response_text)
            except json.JSONDecodeError:
                raise RuntimeError(
                    f"Gemini ASR returned an invalid response: HTTP {response.status}"
                )
            if response.status != 200:
                error = response_data.get("error", {}).get(
                    "message", f"HTTP {response.status}"
                )
                raise RuntimeError(f"Gemini ASR request failed: {error}")

        transcript_parts = []
        for step in response_data.get("steps", []):
            if step.get("type") != "model_output":
                continue
            for content in step.get("content", []):
                if content.get("type") == "text" and content.get("text"):
                    transcript_parts.append(content["text"])
        transcript = "".join(transcript_parts).strip()
        logger.bind(tag=TAG).debug("Gemini ASR transcription completed")
        return transcript, artifacts.file_path

    async def close(self):
        if self._session is not None and not self._session.closed:
            await self._session.close()
