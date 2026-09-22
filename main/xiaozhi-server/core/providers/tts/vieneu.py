import asyncio
import audioop
import atexit
import io
import math
import threading
import time
import wave
from pathlib import Path
from typing import Optional

import numpy as np

from config.logger import setup_logging
from core.providers.tts.base import TTSProviderBase
from core.providers.tts.dto.dto import SentenceType
from core.utils import textUtils
from core.utils.tts import MarkdownCleaner


TAG = __name__
logger = setup_logging()
_ENGINE_CACHE = {}
_ENGINE_CACHE_LOCK = threading.Lock()


def _close_cached_engines():
    with _ENGINE_CACHE_LOCK:
        engines = list(_ENGINE_CACHE.values())
        _ENGINE_CACHE.clear()
    for engine, _ in engines:
        close = getattr(engine, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                pass


atexit.register(_close_cached_engines)


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
        self.first_segment_chars = max(
            1, int(config.get("first_segment_chars", 32))
        )
        self.volume_gain = float(config.get("volume_gain", 1.0))
        self.mastering = bool(config.get("mastering", True))
        self.target_active_rms_dbfs = float(
            config.get("target_active_rms_dbfs", -10.0)
        )
        self.peak_ceiling_dbfs = float(config.get("peak_ceiling_dbfs", -1.0))
        self.apply_watermark = bool(config.get("apply_watermark", False))
        self.keep_model_warm = bool(config.get("keep_model_warm", True))
        self._resample_state = None

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

        if self.keep_model_warm:
            engine_key = tuple(sorted(options.items()))
            with _ENGINE_CACHE_LOCK:
                cached = _ENGINE_CACHE.get(engine_key)
                if cached is None:
                    cached = (Vieneu(**options), threading.Lock())
                    _ENGINE_CACHE[engine_key] = cached
                self.tts, self._inference_lock = cached
        else:
            self.tts = Vieneu(**options)
            self._inference_lock = threading.Lock()
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

    def _generate_pcm(self, text: str) -> tuple[bytes, int]:
        started_at = time.monotonic()
        sentence_id = getattr(self, "current_sentence_id", None)
        logger.bind(tag=TAG).debug(
            f"VieNeu TTS synth start: sentence_id={sentence_id}, chars={len(text)}"
        )
        with self._inference_lock:
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
        logger.bind(tag=TAG).debug(
            "VieNeu TTS synth complete: "
            f"sentence_id={sentence_id}, "
            f"elapsed_ms={(time.monotonic() - started_at) * 1000:.1f}, "
            f"samples={samples.size}, sample_rate={self.sample_rate}"
        )
        return pcm_data, self.sample_rate

    def _generate_wav(self, text: str) -> bytes:
        pcm_data, sample_rate = self._generate_pcm(text)
        output = io.BytesIO()
        with wave.open(output, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(pcm_data)
        return output.getvalue()

    def _get_segment_text(self):
        segment_text = super()._get_segment_text()
        if segment_text:
            return segment_text
        if not self.is_first_sentence:
            return None

        full_text = "".join(self.tts_text_buff)
        current_text = full_text[self.processed_chars :]
        if len(current_text) < self.first_segment_chars:
            return None

        split_limit = min(len(current_text), self.first_segment_chars)
        split_at = current_text.rfind(" ", 0, split_limit + 1)
        if split_at < max(1, self.first_segment_chars // 2):
            split_at = split_limit

        segment_text_raw = current_text[:split_at]
        self.processed_chars += len(segment_text_raw)
        self.is_first_sentence = False
        return textUtils.get_string_no_punctuation_or_emoji(segment_text_raw)

    def _resample_pcm(
        self, pcm_data: bytes, source_rate: int, target_rate: int
    ) -> bytes:
        if source_rate == target_rate:
            return pcm_data
        resampled, self._resample_state = audioop.ratecv(
            pcm_data, 2, 1, source_rate, target_rate, self._resample_state
        )
        return resampled

    def _start_tts_response(self):
        self._resample_state = None
        self.opus_encoder.reset_state()

    def _finish_tts_response(self, opus_handler):
        if self.conn.audio_format == "opus":
            self.opus_encoder.encode_pcm_to_opus_stream(
                b"", end_of_stream=True, callback=opus_handler
            )
        self._resample_state = None

    def _abort_tts_response(self):
        self._resample_state = None
        self.opus_encoder.reset_state()

    def to_tts_stream(self, text, opus_handler=None):
        original_text = text
        text = MarkdownCleaner.clean_markdown(text)
        if self._correct_words_pattern:
            text = self._correct_words_pattern.sub(
                lambda match: self.correct_words[match.group(0)], text
            )

        max_attempts = self.max_retries + 1
        for attempt in range(max_attempts):
            try:
                started_at = time.monotonic()
                self.conn.mark_turn_metric("tts_infer_start")
                pcm_data, source_rate = self._generate_pcm(text)
                if self.conn.client_abort:
                    self._abort_tts_response()
                    return

                pcm_data = self._resample_pcm(
                    pcm_data,
                    source_rate=source_rate,
                    target_rate=self.conn.sample_rate,
                )
                self.tts_audio_queue.put(
                    (
                        SentenceType.FIRST,
                        None,
                        original_text,
                        getattr(self, "current_sentence_id", None),
                    )
                )

                if self.conn.audio_format == "pcm":
                    opus_handler(pcm_data)
                    return

                first_opus_logged = False
                opus_frames = []

                def handle_opus_frame(opus_data):
                    nonlocal first_opus_logged
                    if not first_opus_logged:
                        self.conn.mark_turn_metric("tts_first_opus")
                        logger.bind(tag=TAG).debug(
                            "VieNeu TTS first Opus frame: "
                            f"sentence_id={getattr(self, 'current_sentence_id', None)}, "
                            f"elapsed_ms={(time.monotonic() - started_at) * 1000:.1f}"
                        )
                        first_opus_logged = True
                        # Release the first frame immediately for minimum audible
                        # latency, then batch the remainder to reduce thread hops.
                        opus_handler(opus_data)
                    else:
                        opus_frames.append(opus_data)

                self.opus_encoder.encode_pcm_to_opus_stream(
                    pcm_data,
                    end_of_stream=False,
                    callback=handle_opus_frame,
                )
                if opus_frames:
                    opus_handler(opus_frames)
                return
            except Exception as error:
                if attempt + 1 < max_attempts:
                    logger.bind(tag=TAG).warning(
                        f"VieNeu TTS attempt {attempt + 1} failed for "
                        f"{original_text}, error: {error}"
                    )
                    continue
                logger.bind(tag=TAG).error(
                    f"VieNeu TTS failed for {original_text}: {error}"
                )
                self.tts_audio_queue.put(
                    (
                        SentenceType.FIRST,
                        None,
                        original_text,
                        getattr(self, "current_sentence_id", None),
                    )
                )

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
            if not self.keep_model_warm:
                close = getattr(tts, "close", None)
                if close is not None:
                    await asyncio.to_thread(close)
        finally:
            await super().close()
