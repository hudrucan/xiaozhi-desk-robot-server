import json
from typing import TYPE_CHECKING

from core.handle.sendAudioHandle import send_status_message

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
TAG = __name__


async def handleAbortMessage(conn: "ConnectionHandler"):
    conn.logger.bind(tag=TAG).info("Abort message received")
    # 设置成打断状态，会自动打断llm、tts任务
    conn.close_after_chat = False
    conn.client_abort = True
    llm_cancelled = conn.cancel_active_llm()
    if llm_cancelled:
        conn.logger.bind(tag=TAG).info("Active LLM request cancelled")
    conn.clear_queues()
    # 打断客户端说话状态
    try:
        await conn.websocket.send(
            json.dumps(
                {"type": "tts", "state": "stop", "session_id": conn.session_id}
            )
        )
    except Exception as error:
        conn.logger.bind(tag=TAG).warning(f"Failed to send TTS stop: {error}")
    try:
        await send_status_message(conn, "clear")
    except Exception as error:
        conn.logger.bind(tag=TAG).warning(f"Failed to clear display status: {error}")
    conn.display_status_phase = None
    conn.clearSpeakStatus()
    conn.complete_turn_metrics("aborted")
    conn.logger.bind(tag=TAG).info("Abort message received-end")
