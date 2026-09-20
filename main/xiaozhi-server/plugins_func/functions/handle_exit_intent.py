from plugins_func.register import register_function, ToolType, ActionResponse, Action
from config.logger import setup_logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

handle_exit_intent_function_desc = {
    "type": "function",
    "function": {
        "name": "handle_exit_intent",
        "description": "Call when the user wants to end the conversation or exit.",
        "parameters": {
            "type": "object",
            "properties": {
                "say_goodbye": {
                    "type": "string",
                    "description": "Optional friendly farewell in the configured language.",
                }
            },
            "required": [],
        },
    },
}


@register_function(
    "handle_exit_intent", handle_exit_intent_function_desc, ToolType.SYSTEM_CTL
)
def handle_exit_intent(conn: "ConnectionHandler", say_goodbye: str | None = None):
    """End the current chat while preserving the existing exit lifecycle."""
    try:
        if say_goodbye is None:
            selected = conn.config.get("selected_module", {})
            tts_language = (
                conn.config.get("TTS", {})
                .get(selected.get("TTS", ""), {})
                .get("language", "")
            )
            asr_language = (
                conn.config.get("ASR", {})
                .get(selected.get("ASR", ""), {})
                .get("language", "")
            )
            language = tts_language or asr_language or conn.config.get("locale", "")
            normalized_language = str(language).strip().lower()
            if normalized_language.startswith("vi") or "việt" in normalized_language:
                say_goodbye = "Tạm biệt, hẹn gặp lại nhé!"
            elif (
                normalized_language.startswith("zh")
                or "中文" in normalized_language
                or "chinese" in normalized_language
                or "mandarin" in normalized_language
            ):
                say_goodbye = "再见，祝您生活愉快！"
            else:
                say_goodbye = "Goodbye, see you next time!"
        if not conn.close_after_chat:
            conn.close_after_chat = True
        logger.bind(tag=TAG).info(f"Exit intent handled: {say_goodbye}")
        return ActionResponse(
            action=Action.RESPONSE, result="Exit intent handled", response=say_goodbye
        )
    except Exception as e:
        logger.bind(tag=TAG).error(f"Failed to handle exit intent: {e}")
        return ActionResponse(
            action=Action.NONE, result="Failed to handle exit intent", response=""
        )
