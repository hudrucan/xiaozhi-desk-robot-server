"""Conservative first-segment boundaries for streamed TTS text."""

import re


_WORDS = re.compile(r"\S+")
_NUMERIC = re.compile(r"[+-]?[0-9]+(?:[.,][0-9]+)*")
_PUNCTUATION = frozenset(",;:.?!，；：。？！")
_SENTENCE_END = frozenset(".?!。？！")
_TRAILING = ",;:.?!，；：。？！)]}\"'”’"


def find_first_segment_boundary(text: str, target_chars: int) -> int:
    """Return an inclusive boundary; never split inside a non-space token."""
    minimum = min(20, target_chars)
    preferred = []
    whitespace = []
    overlong = []
    for word in _WORDS.finditer(text):
        # Wait for whitespace lookahead so a stream-edge period cannot later
        # become part of a decimal, version, IP, URL or email address.
        if word.end() == len(text):
            continue
        token = word.group()
        body = token.rstrip(_TRAILING)
        trailing = token[len(body):]
        size = word.end()
        if body and any(char in _PUNCTUATION for char in trailing):
            if size < minimum and any(char in _SENTENCE_END for char in trailing):
                return size - 1
            if minimum <= size <= target_chars:
                preferred.append(size - 1)
            elif size > target_chars:
                overlong.append(size - 1)
        # Keep a quantity attached to its following unit. No dictionary is
        # needed to protect identifiers: whitespace never bisects a token.
        if minimum <= size <= target_chars and not _NUMERIC.fullmatch(body):
            whitespace.append(size - 1)

    if preferred:
        return min(preferred)
    if len(text) >= target_chars:
        if whitespace:
            return max(whitespace)
        return min(overlong, default=-1)
    return -1
