import asyncio
import time
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

from core.handle.sendAudioHandle import send_tts_message
from core.providers.tts.dto.dto import ContentType, SentenceType, TTSMessageDTO


TAG = __name__
TTS_READY_TIMEOUT_SECONDS = 3.0


async def _wait_for_tts_ready(conn: "ConnectionHandler"):
    deadline = time.monotonic() + TTS_READY_TIMEOUT_SECONDS
    initialization_done = getattr(conn, "components_initialization_done", None)
    if initialization_done is not None and not initialization_done.is_set():
        try:
            await asyncio.wait_for(
                initialization_done.wait(),
                timeout=TTS_READY_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return None

    if getattr(conn, "components_initialization_failed", False):
        return None

    tts = getattr(conn, "tts", None)
    if tts is not None and hasattr(tts, "tts_priority_thread"):
        return tts

    while time.monotonic() < deadline:
        tts = getattr(conn, "tts", None)
        if tts is not None and hasattr(tts, "tts_priority_thread"):
            return tts
        await asyncio.sleep(0.05)
    return None


async def send_fixed_tts_response(conn: "ConnectionHandler", text: str) -> bool:
    """Queue fixed text without entering intent, LLM, or dialogue paths."""
    conn.just_woken_up = True
    if not isinstance(text, str) or not text.strip():
        conn.logger.bind(tag=TAG).warning("Ignoring an empty fixed TTS response")
        return False

    tts = await _wait_for_tts_ready(conn)
    if tts is None:
        conn.logger.bind(tag=TAG).warning(
            "Cannot play fixed TTS response because TTS is not ready"
        )
        return False

    conn.client_abort = False
    sentence_id = uuid.uuid4().hex
    conn.sentence_id = sentence_id

    if not conn.has_active_turn_metrics():
        conn.start_turn_metrics("text")
        conn.mark_turn_metric("input_ready", sentence_id=sentence_id)

    await send_tts_message(conn, "start")
    conn.client_is_speaking = True

    tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=sentence_id,
            sentence_type=SentenceType.FIRST,
            content_type=ContentType.ACTION,
        )
    )
    tts.tts_one_sentence(
        conn,
        ContentType.TEXT,
        content_detail=text,
        sentence_id=sentence_id,
    )
    tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=sentence_id,
            sentence_type=SentenceType.LAST,
            content_type=ContentType.ACTION,
        )
    )
    return True
