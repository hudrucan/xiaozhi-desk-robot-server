import asyncio
import io
import wave
from pathlib import Path

import numpy as np

from core.providers.tts.base import TTSProviderBase


class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.audio_file_type = "wav"
        self.buffer_full_response = config.get("buffer_full_response", True)
        self.speed = float(config.get("speed", 1.0))
        self.silence_scale = float(config.get("silence_scale", 0.2))
        self.speaker_id = int(config.get("speaker_id", 0))

        try:
            import sherpa_onnx
        except ImportError as error:
            raise RuntimeError(
                "Sherpa TTS requires the optional sherpa-onnx package"
            ) from error

        self._sherpa_onnx = sherpa_onnx
        model_dir = Path(config.get("model_dir", ""))
        model = model_dir / config.get("model", "model.onnx")
        tokens = model_dir / config.get("tokens", "tokens.txt")
        data_dir = model_dir / config.get("data_dir", "espeak-ng-data")
        self._require_paths(model, tokens, data_dir)

        tts_config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                    model=str(model),
                    tokens=str(tokens),
                    data_dir=str(data_dir),
                ),
                provider=config.get("provider", "cpu"),
                debug=bool(config.get("debug", False)),
                num_threads=max(1, int(config.get("num_threads", 2))),
            ),
            max_num_sentences=int(config.get("max_num_sentences", 1)),
        )
        if not tts_config.validate():
            raise ValueError("Invalid Sherpa TTS configuration")
        self.tts = sherpa_onnx.OfflineTts(tts_config)

    @staticmethod
    def _require_paths(*paths):
        missing = [str(path) for path in paths if not path.exists()]
        if missing:
            raise FileNotFoundError(
                "Missing Sherpa TTS model path(s): " + ", ".join(missing)
            )

    def _get_segment_text(self):
        if self.buffer_full_response:
            return None
        return super()._get_segment_text()

    def _generate_wav(self, text: str) -> bytes:
        generation_config = self._sherpa_onnx.GenerationConfig()
        generation_config.sid = self.speaker_id
        generation_config.speed = self.speed
        generation_config.silence_scale = self.silence_scale
        audio = self.tts.generate(text, generation_config)
        if len(audio.samples) == 0:
            raise RuntimeError("Sherpa TTS returned no audio")

        samples = np.clip(audio.samples, -1.0, 1.0)
        pcm_data = (samples * 32767.0).astype(np.int16).tobytes()
        output = io.BytesIO()
        with wave.open(output, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(audio.sample_rate)
            wav_file.writeframes(pcm_data)
        return output.getvalue()

    async def text_to_speak(self, text, output_file):
        wav_data = await asyncio.to_thread(self._generate_wav, text)
        if output_file:
            output_path = Path(output_file)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(wav_data)
            return None
        return wav_data
