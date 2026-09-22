import asyncio
import io
import math
import wave
from pathlib import Path
from typing import Optional

import numpy as np

from core.providers.tts.base import TTSProviderBase


class TTSProvider(TTSProviderBase):
    """VieNeu v3 Nano adapter using the optional local ONNX runtime."""

    DEFAULT_SAMPLE_RATE = 24000

    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.audio_file_type = "wav"
        self.voice = config.get("voice") or None
        self.steps = max(1, int(config.get("steps", 8)))
        self.cfg = float(config.get("cfg", 3.0))
        self.sway = float(config.get("sway", -1.0))
        self.speed = float(config.get("speed", 1.0))
        configured_seed = config.get("seed")
        self.seed: Optional[int] = (
            None if configured_seed in (None, "") else int(configured_seed)
        )
        self.max_chars = max(1, int(config.get("max_chars", 140)))
        self.volume_gain = float(config.get("volume_gain", 1.0))
        self.mastering = bool(config.get("mastering", True))
        self.target_active_rms_dbfs = float(
            config.get("target_active_rms_dbfs", -10.0)
        )
        self.peak_ceiling_dbfs = float(config.get("peak_ceiling_dbfs", -1.0))
        self.apply_watermark = bool(config.get("apply_watermark", False))

        if not all(
            math.isfinite(value)
            for value in (
                self.cfg,
                self.sway,
                self.speed,
                self.volume_gain,
                self.target_active_rms_dbfs,
                self.peak_ceiling_dbfs,
            )
        ):
            raise ValueError("VieNeu numeric settings must be finite")
        if self.cfg < 0:
            raise ValueError("VieNeu cfg must not be negative")
        if self.speed <= 0:
            raise ValueError("VieNeu speed must be positive")
        if self.volume_gain < 0:
            raise ValueError("VieNeu volume_gain must not be negative")
        if self.target_active_rms_dbfs >= self.peak_ceiling_dbfs:
            raise ValueError("VieNeu active RMS target must be below the peak ceiling")
        if self.peak_ceiling_dbfs > 0:
            raise ValueError("VieNeu peak ceiling must not exceed 0 dBFS")

        try:
            from vieneu import Vieneu
        except ImportError as error:
            raise RuntimeError(
                "VieNeu TTS requires the optional vieneu package"
            ) from error

        options = {
            "mode": "v3nano",
            "threads": max(0, int(config.get("threads", 0))),
        }
        model_repo = config.get("model_repo")
        model_dir = config.get("model_dir")
        hf_token = config.get("hf_token")
        if model_repo:
            options["backbone_repo"] = model_repo
        if model_dir:
            options["onnx_dir"] = model_dir
        if hf_token:
            options["hf_token"] = hf_token

        self.tts = Vieneu(**options)
        self.sample_rate = int(
            getattr(self.tts, "sample_rate", self.DEFAULT_SAMPLE_RATE)
        )
        if self.sample_rate <= 0:
            raise ValueError("VieNeu returned an invalid sample rate")

    @staticmethod
    def _dbfs_to_amplitude(dbfs: float) -> float:
        return 10.0 ** (dbfs / 20.0)

    def _master_samples(self, samples: np.ndarray) -> np.ndarray:
        samples = np.nan_to_num(samples, nan=0.0, posinf=1.0, neginf=-1.0)
        if not self.mastering:
            return np.clip(samples * self.volume_gain, -1.0, 1.0)

        active = np.abs(samples) >= self._dbfs_to_amplitude(-50.0)
        if np.any(active):
            active_rms = float(np.sqrt(np.mean(np.square(samples[active]))))
            if active_rms > 0:
                target_rms = self._dbfs_to_amplitude(self.target_active_rms_dbfs)
                samples = samples * (target_rms / active_rms)

        samples = samples * self.volume_gain
        ceiling = self._dbfs_to_amplitude(self.peak_ceiling_dbfs)
        knee = ceiling * self._dbfs_to_amplitude(-6.0)
        magnitude = np.abs(samples)
        above_knee = magnitude > knee
        if np.any(above_knee):
            span = ceiling - knee
            magnitude[above_knee] = knee + span * np.tanh(
                (magnitude[above_knee] - knee) / span
            )
            samples = np.copysign(magnitude, samples)
        return np.clip(samples, -ceiling, ceiling)

    def _generate_wav(self, text: str) -> bytes:
        audio = self.tts.infer(
            text,
            voice=self.voice,
            steps=self.steps,
            cfg=self.cfg,
            sway=self.sway,
            speed=self.speed,
            seed=self.seed,
            max_chars=self.max_chars,
            apply_watermark=self.apply_watermark,
        )
        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        if samples.size == 0:
            raise RuntimeError("VieNeu TTS returned no audio")

        pcm_data = (
            self._master_samples(samples) * 32767.0
        ).astype("<i2").tobytes()
        output = io.BytesIO()
        with wave.open(output, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(self.sample_rate)
            wav_file.writeframes(pcm_data)
        return output.getvalue()

    async def text_to_speak(self, text: str, output_file: Optional[str]):
        wav_data = await asyncio.to_thread(self._generate_wav, text)
        if output_file:
            output_path = Path(output_file)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(wav_data)
            return None
        return wav_data

    async def close(self):
        tts = self.tts
        self.tts = None
        try:
            close = getattr(tts, "close", None)
            if close is not None:
                await asyncio.to_thread(close)
        finally:
            await super().close()
