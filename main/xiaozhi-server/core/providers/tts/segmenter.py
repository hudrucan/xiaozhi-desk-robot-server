"""Shared, provider-free TTS boundary policy for local and cluster turns."""
from core.utils import text_utils
from core.utils.soundbank_text import normalize_soundbank_text
from .first_segment import find_first_segment_boundary


class SegmentBoundaryPolicy:
    def _get_segment_text(self):
        # Join streamed text and inspect only the unprocessed suffix.
        full_text = "".join(self.tts_text_buff)
        current_text = full_text[self.processed_chars :]

        # Use shorter boundaries for the first segment, or for every segment
        # when explicitly enabled by the provider configuration.
        punctuations_to_use = (
            self.first_sentence_punctuations
            if self.is_first_sentence or self.split_on_all_punctuations
            else self.punctuations
        )

        last_punct_pos = self._find_segment_boundary(
            current_text, punctuations_to_use
        )
        if self.is_first_sentence and self.first_segment_chars:
            preserve_soundbank = False
            if self._static_soundbank_enabled:
                legacy_segment = current_text[:last_punct_pos + 1]
                legacy_key = normalize_soundbank_text(legacy_segment)
                pending_key = normalize_soundbank_text(current_text)
                # Keep known cached phrases intact while their text arrives.
                preserve_soundbank = (
                    legacy_key in self._static_soundbank_entries
                    or bool(pending_key) and any(
                        key.startswith(pending_key)
                        for key in self._static_soundbank_entries
                    )
                )
            if not preserve_soundbank:
                last_punct_pos = find_first_segment_boundary(
                    current_text, self.first_segment_chars
                )

        if last_punct_pos != -1:
            segment_text_raw = current_text[: last_punct_pos + 1]
            segment_text = text_utils.clean_text_segment(
                segment_text_raw
            )
            self.processed_chars += len(segment_text_raw)

            # Allow a shorter first segment to reduce time to first audio.
            if self.is_first_sentence and (
                segment_text or not self.first_segment_chars
            ):
                self.is_first_sentence = False

            return segment_text
        elif self.tts_stop_request and current_text:
            segment_text = text_utils.clean_text_segment(current_text)
            self.is_first_sentence = True
            return segment_text
        else:
            return None

    def _find_segment_boundary(
        self, text, punctuations, prefer_latest=False
    ):
        """Return a safe streaming boundary while preserving split policy."""
        punctuation_set = set(punctuations)
        last_safe_position = {}
        for index, char in enumerate(text):
            if char not in punctuation_set:
                continue

            previous_char = text[index - 1] if index > 0 else ""
            next_char = text[index + 1] if index + 1 < len(text) else ""

            # Keep decimal numbers, clock values, and digit grouping intact.
            if (
                char in {".", ",", ":"}
                and previous_char.isdigit()
                and next_char.isdigit()
            ):
                continue
            if (
                char in {".", ",", ":"}
                and previous_char.isdigit()
                and not next_char
                and not self.tts_stop_request
            ):
                continue

            if char == ".":
                # A period at the current stream edge may still become a
                # decimal or URL once the next token arrives. The final flush
                # below will emit it if no more text follows.
                if not next_char and not self.tts_stop_request:
                    continue
                # Do not split hostnames, versions, or compact identifiers.
                if (
                    next_char
                    and previous_char.isalnum()
                    and next_char.isalnum()
                ):
                    continue

            # Preserve the previous splitter's behavior: use the last safe
            # occurrence of each punctuation type, then select the earliest
            # candidate across punctuation types.
            last_safe_position[char] = index
        positions = last_safe_position.values()
        if prefer_latest:
            return max(positions, default=-1)
        return min(positions, default=-1)
