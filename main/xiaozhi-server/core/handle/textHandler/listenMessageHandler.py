import time
import asyncio
from typing import Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

from core.providers.asr.dto.dto import InterfaceType
from core.handle.receiveAudioHandle import startToChat
from core.handle.sendAudioHandle import send_stt_message, send_tts_message
from core.handle.textMessageHandler import TextMessageHandler
from core.handle.textMessageType import TextMessageType
from core.utils.util import remove_punctuation_and_length


TAG = __name__

class ListenTextMessageHandler(TextMessageHandler):
    """Listen消息处理器"""

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.LISTEN

    async def handle(self, conn: "ConnectionHandler", msg_json: Dict[str, Any]) -> None:
        if "mode" in msg_json:
            conn.client_listen_mode = msg_json["mode"]
            conn.logger.bind(tag=TAG).debug(
                f"Client listening mode: {conn.client_listen_mode}"
            )
        if msg_json["state"] == "start":
            last_tts_stop_sent_at = getattr(conn, "last_tts_stop_sent_at", None)
            if last_tts_stop_sent_at is not None:
                resume_delay_ms = (time.monotonic() - last_tts_stop_sent_at) * 1000
                conn.logger.bind(tag=TAG).info(
                    f"Listening resumed {resume_delay_ms:.1f} ms after TTS stop"
                )
                conn.last_tts_stop_sent_at = None
            # 设备从播放模式切回录音模式,清除所有音频状态和缓冲区
            conn.reset_audio_states()
        elif msg_json["state"] == "stop":
            # 收到stop但asr未初始化，跳过处理
            if conn.asr is None:
                return

            if not conn.has_active_turn_metrics():
                conn.start_turn_metrics("voice")
            conn.mark_turn_metric("speech_end")
            conn.client_voice_stop = True
            if conn.asr.interface_type == InterfaceType.STREAM:
                # 流式模式下，发送结束请求
                asyncio.create_task(conn.asr._send_stop_request())
            else:
                # 非流式模式：直接触发ASR识别
                if len(conn.asr_audio) > 0:
                    asr_audio_task = conn.asr_audio.copy()
                    conn.reset_audio_states()

                    if len(asr_audio_task) > 0:
                        await conn.asr.handle_voice_stop(conn, asr_audio_task)
        elif msg_json["state"] == "detect":
            conn.client_have_voice = False
            conn.reset_audio_states()
            if "text" in msg_json:
                conn.last_activity_time = time.time() * 1000
                original_text = msg_json["text"]  # 保留原始文本

                if msg_json.get("input_mode") == "text":
                    if not isinstance(original_text, str) or not original_text.strip():
                        conn.logger.bind(tag=TAG).warning(
                            "Ignoring typed input with empty or invalid text"
                        )
                        return
                    conn.just_woken_up = False

                    components_ready = getattr(conn, "components_ready", None)
                    server_ready = (
                        components_ready is not None
                        and components_ready.is_set()
                    )
                    mcp_client = getattr(conn, "mcp_client", None)
                    mcp_ready = not mcp_client or await mcp_client.is_ready()
                    if not server_ready or not mcp_ready:
                        if getattr(conn, "pending_typed_input", None) is None:
                            conn.pending_typed_input = original_text
                            conn.logger.bind(tag=TAG).info(
                                "Deferring typed input until initialization completes"
                            )
                        else:
                            conn.logger.bind(tag=TAG).warning(
                                "Ignoring typed input while another typed input is waiting"
                            )
                        return

                    await startToChat(
                        conn, original_text, check_wakeup_word=False
                    )
                    return

                filtered_len, filtered_text = remove_punctuation_and_length(
                    original_text
                )

                configured_wake_words = conn.config.get("wakeup_words", [])
                normalized_wake_words = {
                    remove_punctuation_and_length(wake_word)[1].casefold()
                    for wake_word in configured_wake_words
                    if isinstance(wake_word, str)
                }
                is_wakeup_words = filtered_text.casefold() in normalized_wake_words
                # 是否开启唤醒词回复
                enable_greeting = conn.config.get("enable_greeting", True)

                if is_wakeup_words and not enable_greeting:
                    # 如果是唤醒词，且关闭了唤醒词回复，就不用回答
                    await send_stt_message(conn, original_text)
                    await send_tts_message(conn, "stop", None)
                    conn.client_is_speaking = False
                elif is_wakeup_words:
                    conn.just_woken_up = True
                    await startToChat(
                        conn, conn.config.get("wakeup_greeting", "Hello")
                    )
                else:
                    conn.just_woken_up = True
                    # 否则需要LLM对文字内容进行答复
                    await startToChat(conn, original_text)
