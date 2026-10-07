"""Pure wake-label and command matching shared by both server entrypoints."""
import re

def remove_punctuation_and_length(text):
    # Preserve existing full-width and ASCII punctuation normalization.
    full_width_punctuations = (
        "！＂＃＄％＆＇（）＊＋，－。／：；＜＝＞？＠［＼］＾＿｀｛｜｝～"
    )
    half_width_punctuations = r'!"#$%&\'()*+,-./:;<=>?@[\]^_`{|}~'
    space = " "  # ASCII space
    full_width_space = "　"  # Full-width space

    # Strip punctuation and spaces before exact command comparison.
    result = "".join(
        [
            char
            for char in text
            if char not in full_width_punctuations
            and char not in half_width_punctuations
            and char not in space
            and char not in full_width_space
        ]
    )

    if result == "Yeah":
        return 0, ""
    return len(result), result

def matches_wakeup_word(text, configured_wake_words):
    """Match firmware wake labels, including legacy `A or B` forms."""
    if not isinstance(text, str):
        return False

    if isinstance(configured_wake_words, str):
        configured_wake_words = [configured_wake_words]
    if not isinstance(configured_wake_words, (list, tuple, set)):
        return False

    def normalized_variants(value):
        if not isinstance(value, str):
            return set()
        variants = [value]
        variants.extend(
            re.split(r"\s+(?:or|hoặc)\s+", value, flags=re.IGNORECASE)
        )
        return {
            normalized.casefold()
            for _, normalized in (
                remove_punctuation_and_length(variant.strip())
                for variant in variants
                if variant.strip()
            )
            if normalized
        }

    configured_variants = set()
    for wake_word in configured_wake_words:
        configured_variants.update(normalized_variants(wake_word))

    return bool(normalized_variants(text) & configured_variants)
