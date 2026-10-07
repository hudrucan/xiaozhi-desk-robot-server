"""Isolated warm Sherpa kernel; shared text/mastering, no server runtime."""
import re
import logging
from pathlib import Path

from .sherpa_samples import SherpaSampleProcessing
from .sherpa_native_logs import generate_without_native_log_spam
from .vietnamese_normalizer import VietnameseTTSNormalizer
from core.utils.tts_text import MarkdownCleaner
from core.cluster.tts_config import verify_assets


class SherpaSegmentEngine(SherpaSampleProcessing):
    _INTEGER_PATTERN = re.compile(r'(?<![\w.])[0-9]+(?![\w.])')

    def __init__(self, bundle):
        import sherpa_onnx
        verify_assets(bundle)
        config = bundle['options']
        self.config, self.sdk = config, sherpa_onnx
        for name in ('speaker_id', 'speed', 'silence_scale', 'volume_gain', 'mastering',
                'target_active_rms_dbfs', 'peak_ceiling_dbfs', 'number_language'):
            setattr(self, name, config[name])
        self._text_normalizer = VietnameseTTSNormalizer() if config['text_normalizer'] == 'vietnormalizer' else None
        self._num2words = None
        if self._text_normalizer is None and self.number_language:
            from num2words import num2words
            num2words(0, lang=self.number_language)
            self._num2words = num2words
        self.corrections = dict(word.split('|', 1) for word in config['correct_words'])
        self.pattern = re.compile('|'.join(re.escape(k) for k in sorted(self.corrections, key=len, reverse=True))) if self.corrections else None
        root = Path(bundle['model_root'])
        native = sherpa_onnx.OfflineTtsConfig(model=sherpa_onnx.OfflineTtsModelConfig(
            vits=sherpa_onnx.OfflineTtsVitsModelConfig(model=str(root / config['model']),
                tokens=str(root / config['tokens']), data_dir=str(root / config['data_dir']),
                noise_scale=config['noise_scale'], noise_scale_w=config['noise_scale_w'], length_scale=config['length_scale']),
            provider='cpu', debug=False, num_threads=config['num_threads']), max_num_sentences=config['max_num_sentences'])
        if not native.validate():
            raise ValueError('Invalid Sherpa segment configuration')
        self.tts = sherpa_onnx.OfflineTts(native)
        if (self.tts.num_speakers > 0 and not 0 <= self.speaker_id < self.tts.num_speakers
                or self.tts.num_speakers <= 0 and self.speaker_id != 0):
            raise ValueError('Invalid selected Sherpa speaker')

    def generate(self, text):
        text = MarkdownCleaner.clean_markdown(text)
        if self.pattern:
            text = self.pattern.sub(lambda match: self.corrections[match.group()], text)
        spoken = self._text_normalizer.normalize(text) if self._text_normalizer else self._normalize_numbers(text)
        generation = self.sdk.GenerationConfig()
        generation.sid, generation.speed, generation.silence_scale = self.speaker_id, self.speed, self.silence_scale
        audio = generate_without_native_log_spam(lambda: self.tts.generate(spoken, generation),
            logging.getLogger('xiaozhi.worker.tts').debug)
        samples = self._master_samples(audio.samples)
        return (samples * 32767.0).astype('<i2').tobytes(), audio.sample_rate
