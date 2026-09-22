import json
import unicodedata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler


TAG = __name__

DEFAULT_EMOJI = "🙂"
DEFAULT_EMOTION = "happy"

EMOTION_BY_EMOJI = {
    "😶": "neutral",
    "🙂": "happy",
    "😑": "bored",
    "😆": "laughing",
    "😂": "funny",
    "😔": "sad",
    "😠": "angry",
    "😭": "crying",
    "😍": "loving",
    "😳": "embarrassed",
    "😲": "surprised",
    "😱": "shocked",
    "🤔": "thinking",
    "😉": "winking",
    "😎": "cool",
    "😌": "relaxed",
    "🤤": "delicious",
    "😘": "kissy",
    "😏": "confident",
    "😴": "sleepy",
    "😜": "silly",
    "🙄": "confused",
    "🤨": "suspicious",
    "🫨": "shake",
}

SUPPORTED_EMOTION_EMOJIS = tuple(EMOTION_BY_EMOJI)

EMOJI_CODE_POINT_RANGES = (
    (0x1F1E6, 0x1F1FF),  # Regional indicator symbols used by flags.
    (0x1F300, 0x1F5FF),
    (0x1F600, 0x1F64F),
    (0x1F680, 0x1F6FF),
    (0x1F900, 0x1F9FF),
    (0x1FA70, 0x1FAFF),
    (0x2600, 0x26FF),
    (0x2700, 0x27BF),
)

EMOJI_SEQUENCE_CODE_POINTS = {
    0x200D,  # Zero-width joiner.
    0x20E3,  # Combining enclosing keycap.
    0xFE0F,  # Emoji variation selector.
}


def is_emoji_character(char: str) -> bool:
    """Return whether a single character is an emoji or emoji-sequence component."""
    if not isinstance(char, str) or len(char) != 1:
        return False

    code_point = ord(char)
    if code_point in EMOJI_SEQUENCE_CODE_POINTS:
        return True
    return any(
        start <= code_point <= end
        for start, end in EMOJI_CODE_POINT_RANGES
    )


def is_edge_separator(char: str) -> bool:
    """Return whether a character should be trimmed from a spoken segment edge."""
    if not isinstance(char, str) or len(char) != 1:
        return False
    return (
        char.isspace()
        or unicodedata.category(char).startswith("P")
        or is_emoji_character(char)
    )


def strip_edge_separators(text: str) -> str:
    """Trim whitespace, punctuation and emoji from both ends of text."""
    if not isinstance(text, str) or not text:
        return ""

    start = 0
    end = len(text)
    while start < end and is_edge_separator(text[start]):
        start += 1
    while end > start and is_edge_separator(text[end - 1]):
        end -= 1
    return text[start:end]


def remove_emojis(text: str) -> str:
    """Remove emoji while preserving line boundaries as spaces."""
    if not isinstance(text, str) or not text:
        return ""

    normalized_text = (
        text.replace("\r\n", " ")
        .replace("\r", " ")
        .replace("\n", " ")
    )
    return "".join(
        char for char in normalized_text if not is_emoji_character(char)
    )


def extract_emotion(text: str) -> tuple[str, str]:
    """Extract the first supported emotion marker from an LLM response."""
    if isinstance(text, str):
        for char in text:
            emotion = EMOTION_BY_EMOJI.get(char)
            if emotion:
                return char, emotion
    return DEFAULT_EMOJI, DEFAULT_EMOTION


async def send_emotion_message(conn: "ConnectionHandler", text: str) -> None:
    """Send the response emotion using the existing Xiaozhi WebSocket contract."""
    emoji, emotion = extract_emotion(text)
    try:
        await conn.websocket.send(
            json.dumps(
                {
                    "type": "llm",
                    "text": emoji,
                    "emotion": emotion,
                    "session_id": conn.session_id,
                }
            )
        )
    except Exception as error:
        conn.logger.bind(tag=TAG).warning(
            f"Failed to send emotion emoji: {error}"
        )
