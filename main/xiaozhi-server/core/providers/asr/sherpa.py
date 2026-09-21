import asyncio
import os
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

from config.logger import setup_logging
from core.providers.asr.base import ASRProviderBase
from core.providers.asr.dto.dto import InterfaceType


TAG = __name__
logger = setup_logging()


class ASRProvider(ASRProviderBase):
    def __init__(self, config: dict, delete_audio_file: bool):
        super().__init__()
        self.interface_type = InterfaceType.LOCAL
        self.delete_audio_file = delete_audio_file
        self.sentence_case = config.get("sentence_case", True)
        self.output_dir = config.get("output_dir", "tmp/")
        os.makedirs(self.output_dir, exist_ok=True)

        try:
            import sherpa_onnx
        except ImportError as error:
            raise RuntimeError(
                "Sherpa ASR requires the optional sherpa-onnx package"
            ) from error

        model_dir = Path(config.get("model_dir", ""))
        encoder = model_dir / config.get("encoder", "encoder.int8.onnx")
        decoder = model_dir / config.get("decoder", "decoder.onnx")
        joiner = model_dir / config.get("joiner", "joiner.int8.onnx")
        tokens = model_dir / config.get("tokens", "tokens.txt")
        self._require_files(encoder, decoder, joiner, tokens)

        self.recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(encoder),
            decoder=str(decoder),
            joiner=str(joiner),
            tokens=str(tokens),
            num_threads=max(1, int(config.get("num_threads", 2))),
            sample_rate=16000,
            feature_dim=80,
            decoding_method=config.get("decoding_method", "greedy_search"),
            debug=bool(config.get("debug", False)),
        )

    @staticmethod
    def _require_files(*paths):
        missing = [str(path) for path in paths if not path.is_file()]
        if missing:
            raise FileNotFoundError(
                "Missing Sherpa ASR model file(s): " + ", ".join(missing)
            )

    def _decode(self, pcm_bytes: bytes) -> str:
        samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
        samples /= 32768.0
        stream = self.recognizer.create_stream()
        stream.accept_waveform(16000, samples)
        self.recognizer.decode_stream(stream)
        text = stream.result.text.strip()
        if not text or not self.sentence_case:
            return text
        text = text.lower()
        return text[0].upper() + text[1:]

    async def speech_to_text(
        self,
        opus_data: List[bytes],
        session_id: str,
        artifacts: Optional[ASRProviderBase.AudioArtifacts] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        if artifacts is None or not artifacts.pcm_bytes:
            return "", None

        text = await asyncio.to_thread(self._decode, artifacts.pcm_bytes)
        logger.bind(tag=TAG).debug("Sherpa ASR transcription completed")
        return text, artifacts.file_path
