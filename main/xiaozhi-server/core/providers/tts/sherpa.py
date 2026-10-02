import asyncio
import audioop
import io
import math
import os
import re
import tempfile
import threading
import time
import wave
from collections import Counter
from pathlib import Path

import numpy as np

from config.logger import setup_logging
from core.providers.tts.base import TTSProviderBase
from core.providers.tts.dto.dto import SentenceType
from core.providers.tts.vietnamese_normalizer import VietnameseTTSNormalizer
from core.utils.tts import MarkdownCleaner


TAG = __name__
logger = setup_logging()
_ENGINE_CACHE = {}
_ENGINE_CACHE_LOCK = threading.Lock()
_NATIVE_STDERR_LOCK = threading.Lock()
_UNKNOWN_PHONEME_LOG = re.compile(
    rb".*piper-phonemize-lexicon\.cc:PiperPhonemesToIdsVits:\d+ "
    rb"Skip unknown phonemes\. Unicode codepoint: \\U\+([0-9A-Fa-f]+)\."
)


def _generate_without_native_log_spam(generate):
    """Run Sherpa while retaining every native error except known log spam."""
    with _NATIVE_STDERR_LOCK:
        original_stderr = os.dup(2)
        skipped = Counter()
        with tempfile.TemporaryFile() as captured:
            try:
                os.dup2(captured.fileno(), 2)
                result = generate()
            finally:
                os.dup2(original_stderr, 2)
                captured.seek(0)
                retained = []
                for line in captured.readlines():
                    match = _UNKNOWN_PHONEME_LOG.fullmatch(line.strip())
                    if match:
                        skipped[match.group(1).decode("ascii").upper()] += 1
                    elif line.strip():
                        retained.append(line)
                if retained:
                    os.write(original_stderr, b"".join(retained))
                os.close(original_stderr)

        if skipped:
            summary = ", ".join(
                f"U+{codepoint} x{count}"
                for codepoint, count in sorted(skipped.items())
            )
            logger.bind(tag=TAG).debug(
                f"Sherpa skipped unsupported phonemes: {summary}"
            )
        return result


class TTSProvider(TTSProviderBase):
    _INTEGER_PATTERN = re.compile(r"(?<![\w.])[0-9]+(?![\w.])")

    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.audio_file_type = "wav"
        self.buffer_full_response = config.get("buffer_full_response", True)
        self.keep_model_warm = bool(config.get("keep_model_warm", True))
        self.speed = float(config.get("speed", 1.0))
        self.silence_scale = float(config.get("silence_scale", 0.2))
        self.speaker_id = int(config.get("speaker_id", 0))
        self.noise_scale = float(config.get("noise_scale", 0.667))
        self.noise_scale_w = float(config.get("noise_scale_w", 0.8))
        self.length_scale = float(config.get("length_scale", 1.0))
        for name in ("noise_scale", "noise_scale_w"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a non-negative finite number")
        if not math.isfinite(self.length_scale) or self.length_scale <= 0:
            raise ValueError("length_scale must be a positive finite number")
        self.volume_gain = max(0.0, float(config.get("volume_gain", 1.0)))
        self.mastering = bool(config.get("mastering", False))
        self.target_active_rms_dbfs = float(
            config.get("target_active_rms_dbfs", -11.0)
        )
        self.peak_ceiling_dbfs = float(config.get("peak_ceiling_dbfs", -1.0))
        if not all(
            math.isfinite(value)
            for value in (
                self.target_active_rms_dbfs,
                self.peak_ceiling_dbfs,
            )
        ):
            raise ValueError("Sherpa TTS mastering settings must be finite")
        if self.target_active_rms_dbfs >= self.peak_ceiling_dbfs:
            raise ValueError(
                "Sherpa TTS active RMS target must be below the peak ceiling"
            )
        if self.peak_ceiling_dbfs > 0:
            raise ValueError("Sherpa TTS peak ceiling must not exceed 0 dBFS")
        self.number_language = config.get("number_language")
        self._resample_state = None

        self._num2words = None
        self._text_normalizer = None
        normalizer = config.get("text_normalizer")
        if normalizer == "vietnormalizer":
            self._text_normalizer = VietnameseTTSNormalizer()
        elif normalizer not in (None, "", "none"):
            raise ValueError("text_normalizer must be 'none' or 'vietnormalizer'")
        if self._text_normalizer is None and self.number_language:
            try:
                from num2words import num2words
            except ImportError as error:
                raise RuntimeError(
                    "Sherpa TTS number normalization requires the optional "
                    "num2words package"
                ) from error
            num2words(0, lang=self.number_language)
            self._num2words = num2words

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
                    noise_scale=self.noise_scale,
                    noise_scale_w=self.noise_scale_w,
                    length_scale=self.length_scale,
                ),
                provider=config.get("provider", "cpu"),
                debug=bool(config.get("debug", False)),
                num_threads=max(1, int(config.get("num_threads", 2))),
            ),
            max_num_sentences=int(config.get("max_num_sentences", 1)),
        )
        if not tts_config.validate():
            raise ValueError("Invalid Sherpa TTS configuration")
        if self.keep_model_warm:
            # Cache only the native engine; queues and turn state remain local.
            cache_key = (
                str(model.resolve()),
                str(tokens.resolve()),
                str(data_dir.resolve()),
                self.noise_scale,
                self.noise_scale_w,
                self.length_scale,
                config.get("provider", "cpu"),
                bool(config.get("debug", False)),
                max(1, int(config.get("num_threads", 2))),
                int(config.get("max_num_sentences", 1)),
            )
            with _ENGINE_CACHE_LOCK:
                self.tts = _ENGINE_CACHE.get(cache_key)
                if self.tts is None:
                    self.tts = sherpa_onnx.OfflineTts(tts_config)
                    _ENGINE_CACHE[cache_key] = self.tts
                    logger.bind(tag=TAG).info("Sherpa TTS model loaded and kept warm")
                else:
                    logger.bind(tag=TAG).info("Sherpa TTS warm model reused")
        else:
            self.tts = sherpa_onnx.OfflineTts(tts_config)
        num_speakers = self.tts.num_speakers
        if num_speakers > 0:
            if not 0 <= self.speaker_id < num_speakers:
                raise ValueError(
                    f"speaker_id must be in range 0..{num_speakers - 1}"
                )
        elif self.speaker_id != 0:
            raise ValueError(
                "speaker_id must be 0 for a single-speaker model"
            )

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

    def _normalize_numbers(self, text: str) -> str:
        if self._num2words is None:
            return text
        return self._INTEGER_PATTERN.sub(
            lambda match: self._num2words(
                int(match.group(0)), lang=self.number_language
            ),
            text,
        )

    @staticmethod
    def _dbfs_to_amplitude(dbfs: float) -> float:
        return 10.0 ** (dbfs / 20.0)

    def _master_samples(self, samples: np.ndarray) -> np.ndarray:
        samples = np.asarray(samples, dtype=np.float32)
        if not self.mastering:
            return np.clip(samples * self.volume_gain, -1.0, 1.0)

        samples = np.nan_to_num(samples, nan=0.0, posinf=1.0, neginf=-1.0)
        active = np.abs(samples) >= self._dbfs_to_amplitude(-50.0)
        if np.any(active):
            active_rms = float(
                np.sqrt(np.mean(np.square(samples[active], dtype=np.float64)))
            )
            if active_rms > 0:
                target_rms = self._dbfs_to_amplitude(
                    self.target_active_rms_dbfs
                )
                samples = samples * (target_rms / active_rms)

        samples = samples * self.volume_gain
        ceiling = self._dbfs_to_amplitude(self.peak_ceiling_dbfs)
        if ceiling == 0.0:
            return np.zeros_like(samples)
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
        generation_config = self._sherpa_onnx.GenerationConfig()
        generation_config.sid = self.speaker_id
        generation_config.speed = self.speed
        generation_config.silence_scale = self.silence_scale
        spoken_text = (
            self._text_normalizer.normalize(text)
            if self._text_normalizer is not None
            else self._normalize_numbers(text)
        )

        def generate():
            synth_started_at = time.monotonic()
            if self.conn:
                self.conn.mark_turn_metric("tts_synth_start", sentence_id=sentence_id)
            logger.bind(tag=TAG).debug(
                "Sherpa TTS synth start: "
                f"sentence_id={sentence_id}, chars={len(text)}, "
                f"spoken_chars={len(spoken_text)}, "
                f"prepare_wait_ms={(synth_started_at - started_at) * 1000:.1f}, "
                f"monotonic_s={synth_started_at:.6f}"
            )
            audio = self.tts.generate(spoken_text, generation_config)
            completed_at = time.monotonic()
            if self.conn:
                self.conn.mark_turn_metric("tts_synth_end", sentence_id=sentence_id)
            logger.bind(tag=TAG).debug(
                "Sherpa TTS synth complete: "
                f"sentence_id={sentence_id}, "
                f"elapsed_ms={(completed_at - synth_started_at) * 1000:.1f}, "
                f"samples={len(audio.samples)}, sample_rate={audio.sample_rate}, "
                f"monotonic_s={completed_at:.6f}"
            )
            return audio

        audio = _generate_without_native_log_spam(generate)
        if len(audio.samples) == 0:
            raise RuntimeError("Sherpa TTS returned no audio")

        samples = self._master_samples(audio.samples)
        pcm_data = (samples * 32767.0).astype("<i2").tobytes()
        pcm_ready_at = time.monotonic()
        if self.conn:
            self.conn.mark_turn_metric("tts_first_pcm", sentence_id=sentence_id)
        logger.bind(tag=TAG).debug(
            "Sherpa TTS first PCM available: "
            f"sentence_id={sentence_id}, "
            f"elapsed_ms={(pcm_ready_at - started_at) * 1000:.1f}, "
            f"monotonic_s={pcm_ready_at:.6f}"
        )
        return pcm_data, audio.sample_rate

    def _resample_pcm(
        self, pcm_data: bytes, source_rate: int, target_rate: int
    ) -> bytes:
        if source_rate == target_rate:
            return pcm_data
        resampled, self._resample_state = audioop.ratecv(
            pcm_data, 2, 1, source_rate, target_rate, self._resample_state
        )
        return resampled

    def _generate_wav(self, text: str) -> bytes:
        pcm_data, sample_rate = self._generate_pcm(text)
        output = io.BytesIO()
        with wave.open(output, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(pcm_data)
        return output.getvalue()

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
                pcm_data, source_rate = self._generate_pcm(text)
                if self.conn.client_abort:
                    self._abort_tts_response()
                    return

                target_rate = self.conn.sample_rate
                pcm_data = self._resample_pcm(
                    pcm_data, source_rate=source_rate, target_rate=target_rate
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

                def handle_opus_frame(opus_data):
                    nonlocal first_opus_logged
                    if not first_opus_logged:
                        logger.bind(tag=TAG).debug(
                            "Sherpa TTS first Opus frame: "
                            f"sentence_id={getattr(self, 'current_sentence_id', None)}, "
                            f"elapsed_ms={(time.monotonic() - started_at) * 1000:.1f}, "
                            f"monotonic_s={time.monotonic():.6f}"
                        )
                        first_opus_logged = True
                    opus_handler(opus_data)

                self.opus_encoder.encode_pcm_to_opus_stream(
                    pcm_data,
                    end_of_stream=False,
                    callback=handle_opus_frame,
                )
                return
            except Exception as error:
                if attempt + 1 < max_attempts:
                    logger.bind(tag=TAG).warning(
                        f"Speech generation attempt {attempt + 1} failed for "
                        f"{original_text}, error: {error}"
                    )
                    continue
                logger.bind(tag=TAG).error(
                    f"Speech generation failed for {original_text}; "
                    f"check the model and runtime status: {error}"
                )
                self.tts_audio_queue.put(
                    (
                        SentenceType.FIRST,
                        None,
                        original_text,
                        getattr(self, "current_sentence_id", None),
                    )
                )

    async def text_to_speak(self, text, output_file):
        wav_data = await asyncio.to_thread(self._generate_wav, text)
        if output_file:
            output_path = Path(output_file)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(wav_data)
            return None
        return wav_data
