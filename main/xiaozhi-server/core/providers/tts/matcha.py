import asyncio
import audioop
import io
import json
import math
import re
import threading
import time
import unicodedata
import wave
from pathlib import Path
from typing import Optional

import numpy as np

from config.logger import setup_logging
from core.providers.tts.base import TTSProviderBase
from core.providers.tts.dto.dto import SentenceType
from core.utils import text_utils
from core.utils.tts import MarkdownCleaner


TAG = __name__
logger = setup_logging()
_ENGINE_CACHE = {}
_ENGINE_CACHE_LOCK = threading.Lock()


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version)[:3])


class _MatchaEngine:
    SAMPLE_RATE = 22050
    MEL_CHANNELS = 80
    MEL_MEAN = -5.205414772033691
    MEL_STD = 2.5967071056365967

    _SPECIAL_SYMBOLS = {
        "/": " xuyệt ",
        "\\": " xuyệt ngược ",
        "_": " gạch dưới ",
        "@": " a còng ",
        "#": " thăng ",
        "$": " đô la ",
        "%": " phần trăm ",
        "^": " mũ ",
        "&": " và ",
        "*": " sao ",
        "+": " cộng ",
        "=": " bằng ",
        "<": " nhỏ hơn ",
        ">": " lớn hơn ",
        "|": " hoặc ",
        "~": " khoảng ",
    }

    def __init__(
        self,
        model_dir: Path,
        variant: str,
        encoder_name: str,
        decoder_name: str,
        vocos_name: str,
        prompt_encoder_name: str,
        symbols_name: str,
        num_threads: int,
        provider: str,
    ):
        try:
            import onnxruntime as ort
        except ImportError as error:
            raise RuntimeError("Matcha TTS requires onnxruntime") from error

        if _version_tuple(ort.__version__) < (1, 28, 0):
            raise RuntimeError(
                "Matcha TTS requires onnxruntime 1.28 or newer for Vocos IRFFT"
            )

        variant_dir = model_dir / variant
        self.encoder_path = variant_dir / encoder_name
        self.decoder_path = variant_dir / decoder_name
        self.vocos_path = variant_dir / vocos_name
        self.prompt_encoder_path = model_dir / prompt_encoder_name
        self.symbols_path = model_dir / symbols_name
        self._require_paths(
            self.encoder_path,
            self.decoder_path,
            self.vocos_path,
            self.prompt_encoder_path,
            self.symbols_path,
        )

        with self.symbols_path.open("r", encoding="utf-8") as symbols_file:
            symbols = json.load(symbols_file)
        self.symbol_to_id = symbols.get("_symbol_to_id")
        if not isinstance(self.symbol_to_id, dict):
            raw_symbols = symbols.get("symbols")
            if not isinstance(raw_symbols, list):
                raise ValueError("Invalid Matcha symbols.json")
            self.symbol_to_id = {
                symbol: index for index, symbol in enumerate(raw_symbols)
            }
        if self.symbol_to_id.get("_") != 0:
            raise ValueError("Matcha blank symbol must use token id 0")

        provider_name = provider or "CPUExecutionProvider"
        if provider_name not in ort.get_available_providers():
            raise ValueError(
                f"ONNX Runtime provider is unavailable: {provider_name}"
            )

        session_options = ort.SessionOptions()
        session_options.intra_op_num_threads = max(1, num_threads)
        session_options.inter_op_num_threads = 1
        session_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        session_options.graph_optimization_level = (
            ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        )
        session_options.enable_cpu_mem_arena = True
        session_options.enable_mem_pattern = True
        session_options.log_severity_level = 3
        providers = [provider_name]

        self.encoder = ort.InferenceSession(
            str(self.encoder_path),
            sess_options=session_options,
            providers=providers,
        )
        self.decoder = ort.InferenceSession(
            str(self.decoder_path),
            sess_options=session_options,
            providers=providers,
        )
        self.vocos = ort.InferenceSession(
            str(self.vocos_path),
            sess_options=session_options,
            providers=providers,
        )
        self.prompt_encoder = ort.InferenceSession(
            str(self.prompt_encoder_path),
            sess_options=session_options,
            providers=providers,
        )
        self._vocos_input_name = self.vocos.get_inputs()[0].name
        self._vocos_output_name = self.vocos.get_outputs()[0].name
        self._inference_lock = threading.Lock()

    @staticmethod
    def _require_paths(*paths: Path):
        missing = [str(path) for path in paths if not path.is_file()]
        if missing:
            raise FileNotFoundError(
                "Missing Matcha TTS model path(s): " + ", ".join(missing)
            )

    def normalize_text(self, text: str) -> str:
        text = unicodedata.normalize("NFC", text).lower()
        for symbol, spoken in self._SPECIAL_SYMBOLS.items():
            text = text.replace(symbol, spoken)
        for source, replacement in (
            ("–", "-"),
            ("—", "-"),
            ("‘", "'"),
            ("’", "'"),
            ("“", '"'),
            ("”", '"'),
        ):
            text = text.replace(source, replacement)

        filtered = []
        for character in text:
            if character in self.symbol_to_id:
                filtered.append(character)
            elif character.isspace():
                filtered.append(" ")
            else:
                filtered.append(" ")
        return re.sub(r"\s+", " ", "".join(filtered)).strip()

    def text_to_sequence(self, text: str) -> np.ndarray:
        token_ids = [
            self.symbol_to_id[character]
            for character in self.normalize_text(text)
            if character in self.symbol_to_id
        ]
        if not token_ids:
            raise ValueError("Matcha TTS input has no supported symbols")

        sequence = np.zeros(len(token_ids) * 2 + 1, dtype=np.int64)
        sequence[1::2] = token_ids
        return sequence.reshape(1, -1)

    @staticmethod
    def _prompt_tail(
        previous_mel: Optional[np.ndarray],
        previous_text: str,
        tail_words: int,
    ) -> Optional[np.ndarray]:
        if previous_mel is None or previous_mel.size == 0 or tail_words <= 0:
            return None
        word_count = max(1, len(previous_text.split()))
        selected_words = min(word_count, max(3, tail_words))
        selected_frames = max(
            30,
            int(previous_mel.shape[-1] * selected_words / word_count),
        )
        selected_frames = min(selected_frames, previous_mel.shape[-1])
        return np.ascontiguousarray(previous_mel[:, :, -selected_frames:])

    @staticmethod
    def _sway_time(value: float, sway: Optional[float]) -> float:
        if sway is None:
            return value
        return value + sway * (math.cos(math.pi * 0.5 * value) - 1.0 + value)

    @staticmethod
    def _fade_edges(samples: np.ndarray, fade_samples: int) -> None:
        if fade_samples <= 0 or samples.size < fade_samples * 2:
            return
        positions = np.arange(fade_samples, dtype=np.float32) / fade_samples
        fade_in = np.square(np.sin(positions * math.pi * 0.5))
        fade_out = np.square(np.cos(positions * math.pi * 0.5))
        samples[:fade_samples] *= fade_in
        samples[-fade_samples:] *= fade_out

    def synthesize(
        self,
        text: str,
        steps: int,
        temperature: float,
        length_scale: float,
        sway: Optional[float],
        seed: Optional[int],
        previous_mel: Optional[np.ndarray] = None,
        previous_text: str = "",
        tail_prompt_words: int = 10,
        use_prompt: bool = True,
        tail_trim_samples: int = 0,
        edge_fade_ms: float = 5.0,
    ) -> tuple[np.ndarray, np.ndarray]:
        with self._inference_lock:
            sequence = self.text_to_sequence(text)
            mu, mask = self.encoder.run(
                ["mu_y", "y_mask"],
                {
                    "x": sequence,
                    "x_lengths": np.asarray(
                        [sequence.shape[-1]], dtype=np.int64
                    ),
                    "length_scale": np.asarray(length_scale, dtype=np.float32),
                },
            )
            mu = np.ascontiguousarray(mu, dtype=np.float32)
            mask = np.ascontiguousarray(mask, dtype=np.float32)

            if use_prompt:
                prompt = self._prompt_tail(
                    previous_mel, previous_text, tail_prompt_words
                )
                if prompt is not None:
                    prompt_condition = self.prompt_encoder.run(
                        ["prompt_cond"], {"mel_prompt": prompt}
                    )[0]
                    mu += np.asarray(prompt_condition, dtype=np.float32)[:, :, None]

            rng = np.random.default_rng(seed)
            flow = rng.normal(
                0.0, temperature, size=mu.shape
            ).astype(np.float32)
            step_count = max(1, steps)
            for step in range(step_count):
                current = step / step_count
                following = (step + 1) / step_count
                time_value = self._sway_time(current, sway)
                step_size = self._sway_time(following, sway) - time_value
                derivative = self.decoder.run(
                    ["dphi_dt"],
                    {
                        "x": flow,
                        "mask": mask,
                        "mu": mu,
                        "t": np.asarray([time_value], dtype=np.float32),
                    },
                )[0]
                derivative = np.asarray(derivative, dtype=np.float32)
                if derivative.shape != flow.shape:
                    raise RuntimeError(
                        "Matcha decoder returned an unexpected mel shape: "
                        f"{derivative.shape}, expected {flow.shape}"
                    )
                flow += np.float32(step_size) * derivative

            mel = np.ascontiguousarray(
                flow * self.MEL_STD + self.MEL_MEAN,
                dtype=np.float32,
            )
            audio = self.vocos.run(
                [self._vocos_output_name],
                {self._vocos_input_name: mel},
            )[0]

        samples = np.nan_to_num(
            np.asarray(audio, dtype=np.float32).reshape(-1),
            nan=0.0,
            posinf=1.0,
            neginf=-1.0,
        )
        # Never discard an entire short segment, even with an oversized trim.
        if 0 < tail_trim_samples < samples.size:
            samples = samples[:-tail_trim_samples].copy()
        fade_samples = int(min(
            self.SAMPLE_RATE * edge_fade_ms / 1000.0, samples.size
        ))
        self._fade_edges(samples, fade_samples)
        return samples, mel


class TTSProvider(TTSProviderBase):
    _INTEGER_PATTERN = re.compile(r"(?<![\w.])[0-9]+(?!\w|\.[0-9])")

    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.audio_file_type = "wav"
        self.steps = max(1, int(config.get("steps", 2)))
        self.temperature = float(config.get("temperature", 0.9))
        self.length_scale = float(config.get("length_scale", 1.0))
        configured_sway = config.get("sway", -1.0)
        self.sway = (
            None if configured_sway in (None, "") else float(configured_sway)
        )
        configured_seed = config.get("seed", 42)
        self.seed = (
            None if configured_seed in (None, "") else int(configured_seed)
        )
        self.use_prompt = bool(config.get("use_prompt", True))
        self.tail_prompt_words = max(0, int(config.get("tail_prompt_words", 10)))
        self.first_segment_chars = max(
            1, int(config.get("first_segment_chars", 32))
        )
        self.volume_gain = float(config.get("volume_gain", 1.0))
        configured_tail_trim = float(config.get("tail_trim_samples", 0))
        if (
            not math.isfinite(configured_tail_trim)
            or configured_tail_trim < 0
            or not configured_tail_trim.is_integer()
        ):
            raise ValueError("Matcha tail_trim_samples must be a non-negative integer")
        self.tail_trim_samples = int(configured_tail_trim)
        self.edge_fade_ms = float(config.get("edge_fade_ms", 5.0))
        self.mastering = bool(config.get("mastering", True))
        self.target_active_rms_dbfs = float(
            config.get("target_active_rms_dbfs", -10.0)
        )
        self.peak_ceiling_dbfs = float(config.get("peak_ceiling_dbfs", -1.0))
        self.keep_model_warm = bool(config.get("keep_model_warm", True))
        self.number_language = config.get("number_language")
        self._resample_state = None
        self._previous_mel = None
        self._previous_text = ""

        for value, name in (
            (self.temperature, "temperature"),
            (self.length_scale, "length_scale"),
            (self.volume_gain, "volume_gain"),
            (self.edge_fade_ms, "edge_fade_ms"),
            (self.target_active_rms_dbfs, "target_active_rms_dbfs"),
            (self.peak_ceiling_dbfs, "peak_ceiling_dbfs"),
        ):
            if not math.isfinite(value):
                raise ValueError(f"Matcha {name} must be finite")
        if self.temperature < 0:
            raise ValueError("Matcha temperature must not be negative")
        if self.sway is not None and not math.isfinite(self.sway):
            raise ValueError("Matcha sway must be finite or None")
        if self.length_scale <= 0:
            raise ValueError("Matcha length_scale must be positive")
        if self.volume_gain < 0:
            raise ValueError("Matcha volume_gain must not be negative")
        if self.edge_fade_ms < 0:
            raise ValueError("Matcha edge_fade_ms must not be negative")
        if self.target_active_rms_dbfs >= self.peak_ceiling_dbfs:
            raise ValueError("Matcha active RMS target must be below the peak ceiling")
        if self.peak_ceiling_dbfs > 0:
            raise ValueError("Matcha peak ceiling must not exceed 0 dBFS")

        self._num2words = None
        if self.number_language:
            try:
                from num2words import num2words
            except ImportError:
                logger.bind(tag=TAG).warning(
                    "num2words is not installed; Matcha TTS will keep digits "
                    "unchanged"
                )
            else:
                num2words(0, lang=self.number_language)
                self._num2words = num2words

        engine_options = {
            "model_dir": Path(config.get("model_dir", "")),
            "variant": config.get("variant", "int8"),
            "encoder_name": config.get("encoder", "matcha_encoder.onnx"),
            "decoder_name": config.get("decoder", "matcha_decoder.onnx"),
            "vocos_name": config.get("vocos", "vocos.onnx"),
            "prompt_encoder_name": config.get(
                "prompt_encoder", "prompt_encoder.onnx"
            ),
            "symbols_name": config.get("symbols", "symbols.json"),
            "num_threads": max(1, int(config.get("num_threads", 4))),
            "provider": config.get("provider", "CPUExecutionProvider"),
        }
        if self.keep_model_warm:
            cache_key = tuple(
                sorted((key, str(value)) for key, value in engine_options.items())
            )
            with _ENGINE_CACHE_LOCK:
                self.engine = _ENGINE_CACHE.get(cache_key)
                if self.engine is None:
                    self.engine = _MatchaEngine(**engine_options)
                    _ENGINE_CACHE[cache_key] = self.engine
        else:
            self.engine = _MatchaEngine(**engine_options)

    def _normalize_numbers(self, text: str) -> str:
        if self._num2words is None:
            return text
        return self._INTEGER_PATTERN.sub(
            lambda match: self._num2words(
                int(match.group(0)), lang=self.number_language
            ),
            text,
        )

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
        return text_utils.clean_text_segment(segment_text_raw)

    @staticmethod
    def _dbfs_to_amplitude(dbfs: float) -> float:
        return 10.0 ** (dbfs / 20.0)

    def _master_samples(self, samples: np.ndarray) -> np.ndarray:
        samples = np.nan_to_num(
            np.asarray(samples, dtype=np.float32),
            nan=0.0,
            posinf=1.0,
            neginf=-1.0,
        )
        if not self.mastering:
            return np.clip(samples * self.volume_gain, -1.0, 1.0)

        # Exclude pauses/near-silence so they cannot cause excessive gain.
        active = np.abs(samples) >= self._dbfs_to_amplitude(-50.0)
        if np.any(active):
            active_rms = float(np.sqrt(
                np.mean(np.square(samples[active], dtype=np.float64))
            ))
            if active_rms > 0:
                target_rms = self._dbfs_to_amplitude(self.target_active_rms_dbfs)
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

    def _generate_pcm(self, text: str, preserve_prompt: bool) -> tuple[bytes, int]:
        started_at = time.monotonic()
        normalized_text = self._normalize_numbers(text)
        previous_mel = self._previous_mel if preserve_prompt else None
        previous_text = self._previous_text if preserve_prompt else ""
        samples, mel = self.engine.synthesize(
            normalized_text,
            steps=self.steps,
            temperature=self.temperature,
            length_scale=self.length_scale,
            sway=self.sway,
            seed=self.seed,
            previous_mel=previous_mel,
            previous_text=previous_text,
            tail_prompt_words=self.tail_prompt_words,
            use_prompt=self.use_prompt and preserve_prompt,
            tail_trim_samples=self.tail_trim_samples,
            edge_fade_ms=self.edge_fade_ms,
        )
        if samples.size == 0:
            raise RuntimeError("Matcha TTS returned no audio")
        if preserve_prompt:
            self._previous_mel = mel
            self._previous_text = normalized_text

        pcm_data = (
            self._master_samples(samples) * 32767.0
        ).astype("<i2").tobytes()
        logger.bind(tag=TAG).debug(
            "Matcha TTS synth complete: "
            f"sentence_id={getattr(self, 'current_sentence_id', None)}, "
            f"elapsed_ms={(time.monotonic() - started_at) * 1000:.1f}, "
            f"samples={samples.size}, sample_rate={self.engine.SAMPLE_RATE}"
        )
        return pcm_data, self.engine.SAMPLE_RATE

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
        pcm_data, sample_rate = self._generate_pcm(text, preserve_prompt=False)
        output = io.BytesIO()
        with wave.open(output, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(pcm_data)
        return output.getvalue()

    def _start_tts_response(self):
        self._resample_state = None
        self._previous_mel = None
        self._previous_text = ""
        self.opus_encoder.reset_state()

    def _finish_tts_response(self, opus_handler):
        if self.conn.audio_format == "opus":
            self.opus_encoder.encode_pcm_to_opus_stream(
                b"", end_of_stream=True, callback=opus_handler
            )
        self._resample_state = None
        self._previous_mel = None
        self._previous_text = ""

    def _abort_tts_response(self):
        self._resample_state = None
        self._previous_mel = None
        self._previous_text = ""
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
                pcm_data, source_rate = self._generate_pcm(
                    text, preserve_prompt=True
                )
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
                            "Matcha TTS first Opus frame: "
                            f"sentence_id={getattr(self, 'current_sentence_id', None)}, "
                            f"elapsed_ms={(time.monotonic() - started_at) * 1000:.1f}"
                        )
                        first_opus_logged = True
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
                        f"Matcha TTS attempt {attempt + 1} failed for "
                        f"{original_text}, error: {error}"
                    )
                    continue
                logger.bind(tag=TAG).error(
                    f"Matcha TTS failed for {original_text}: {error}"
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
        self._previous_mel = None
        self._previous_text = ""
        if not self.keep_model_warm:
            self.engine = None
        await super().close()
