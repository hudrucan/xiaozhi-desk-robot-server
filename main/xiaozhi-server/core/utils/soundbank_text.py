"""Soundbank text matching without configuration or filesystem imports."""
import unicodedata
from core.utils import text_utils


def normalize_soundbank_text(text):
    """Normalize stable segment-edge and whitespace differences."""
    if not isinstance(text, str):
        return ""
    normalized = unicodedata.normalize("NFC", text)
    normalized = text_utils.strip_edge_separators(normalized)
    normalized = " ".join(normalized.split()).strip()
    return normalized.casefold()
