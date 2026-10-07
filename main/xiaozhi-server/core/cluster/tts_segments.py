"""Bounded incremental wrapper around the existing shared splitter policy."""
from core.providers.tts.segmenter import SegmentBoundaryPolicy
from core.utils.text_utils import clean_text_segment
from .llm_protocol import MAX_TEXT_BYTES


class SegmentBuffer(SegmentBoundaryPolicy):
    def __init__(self, options):
        self.tts_text_buff = []
        self.processed_chars = self.bytes = 0
        self.is_first_sentence = True
        self.tts_stop_request = self.finished = False
        self.first_segment_chars = options['first_segment_chars']
        self.split_on_all_punctuations = options['split_on_all_punctuations']
        self.punctuations = ('。', '.', '？', '?', '！', '!', '；', ';', '：')
        self.first_sentence_punctuations = ('，', '~', '、', ',', *self.punctuations)
        self._static_soundbank_enabled = False
        self._static_soundbank_entries = {}

    def append(self, text):
        if self.finished or not isinstance(text, str):
            raise ValueError('Invalid TTS text lifecycle')
        self.bytes += len(text.encode('utf-8'))
        if self.bytes > MAX_TEXT_BYTES:
            raise ValueError('TTS text exceeds turn bound')
        self.tts_text_buff.append(text)

    def pop(self):
        while True:
            previous = self.processed_chars
            text = self._get_segment_text()
            if text:
                return text
            if previous != self.processed_chars:
                continue  # Separator/emoji-only fragments are consumed once.
            if self.finished:
                all_text = ''.join(self.tts_text_buff)
                text = clean_text_segment(all_text[self.processed_chars:])
                self.processed_chars = len(all_text)
                return text or None
            return None
