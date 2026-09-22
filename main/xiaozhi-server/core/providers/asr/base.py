import os
import io
import wave
import uuid
import json
import time
import shutil
import asyncio
import tempfile
import traceback

from abc import ABC, abstractmethod
from config.logger import setup_logging
from core.providers.asr.dto.dto import InterfaceType
from core.handle.receiveAudioHandle import startToChat
from core.utils.util import remove_punctuation_and_length
from core.handle.receiveAudioHandle import handleAudioMessage
from typing import Optional, Tuple, List, NamedTuple, TYPE_CHECKING


if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()


class ASRProviderBase(ABC):
    def __init__(self):
        pass

    # 打开音频通道
    async def open_audio_channels(self, conn: "ConnectionHandler"):
        conn.asr_audio_task = asyncio.create_task(self.asr_audio_consumer(conn))

    # 有序处理ASR音频
    async def asr_audio_consumer(self, conn: "ConnectionHandler"):
        while not conn.stop_event.is_set():
            try:
                message = await conn.asr_audio_queue.get()
                try:
                    await handleAudioMessage(conn, message)
                finally:
                    conn.asr_audio_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.bind(tag=TAG).error(
                    f"Failed to process ASR text: {str(e)}, type: {type(e).__name__}, stack trace: {traceback.format_exc()}"
                )

    # 接收音频
    async def receive_audio(self, conn: "ConnectionHandler", pcm_frame, audio_have_voice):
        if conn.client_listen_mode == "manual":
            # 手动模式：缓存音频用于ASR识别
            conn.asr_audio.append(pcm_frame)
        else:
            # 自动/实时模式：使用VAD检测
            conn.asr_audio.append(pcm_frame)

            # 如果没有语音，且之前也没有声音，缓存部分音频
            if not audio_have_voice and not conn.client_have_voice:
                conn.asr_audio = conn.asr_audio[-10:]
                return

            # 自动模式下通过VAD检测到语音停止时触发识别
            if conn.asr.interface_type != InterfaceType.STREAM and conn.client_voice_stop:
                # 直接使用asr_audio中的PCM数据
                pcm_bytes = b"".join(conn.asr_audio)
                min_audio_ms = max(0, int(conn.config.get("asr_min_audio_ms", 300)))
                min_audio_bytes = 16000 * 2 * min_audio_ms // 1000
                if len(pcm_bytes) >= min_audio_bytes:
                    await self.handle_voice_stop(conn, [pcm_bytes])
                else:
                    conn.complete_turn_metrics("audio_too_short")
                conn.reset_audio_states()

    # 处理语音停止
    async def handle_voice_stop(self, conn: "ConnectionHandler", asr_audio_task: List[bytes]):
        """并行处理ASR和声纹识别"""
        try:
            if not conn.has_active_turn_metrics():
                conn.start_turn_metrics("voice")
            conn.mark_turn_metric("speech_end")
            conn.mark_turn_metric("asr_start")
            total_start_time = time.monotonic()

            # 数据已经是PCM直接使用
            pcm_data = asr_audio_task
            combined_pcm_data = b"".join(pcm_data)

            # 预先准备WAV数据
            wav_data = None
            if conn.voiceprint_provider and combined_pcm_data:
                wav_data = self._pcm_to_wav(combined_pcm_data)
            audio_duration_ms = len(combined_pcm_data) * 1000 / (16000 * 2)

            # 定义ASR任务
            asr_task = self.speech_to_text_wrapper(
                asr_audio_task, conn.session_id
            )

            if conn.voiceprint_provider and wav_data:
                voiceprint_task = conn.voiceprint_provider.identify_speaker(
                    wav_data, conn.session_id
                )
                # 并发等待两个结果
                asr_result, voiceprint_result = await asyncio.gather(
                    asr_task, voiceprint_task, return_exceptions=True
                )
            else:
                asr_result = await asr_task
                voiceprint_result = None

            # 记录识别结果 - 检查是否为异常
            if isinstance(asr_result, Exception):
                logger.bind(tag=TAG).error(f"ASR recognition failed: {asr_result}")
                raw_text = ""
            else:
                raw_text, _ = asr_result

            if isinstance(voiceprint_result, Exception):
                logger.bind(tag=TAG).error(f"Voiceprint recognition failed: {voiceprint_result}")
                speaker_name = ""
            else:
                speaker_name = voiceprint_result

            # 判断 ASR 结果类型
            if isinstance(raw_text, dict):
                # Preserve structured metadata returned by compatible ASR providers.
                if speaker_name:
                    raw_text["speaker"] = speaker_name

                # 记录识别结果
                if raw_text.get("language"):
                    logger.bind(tag=TAG).info(f"Detected language: {raw_text['language']}")
                if raw_text.get("emotion"):
                    logger.bind(tag=TAG).info(f"Detected emotion: {raw_text['emotion']}")
                if raw_text.get("content"):
                    logger.bind(tag=TAG).info(f"Recognized text: {raw_text['content']}")
                if speaker_name:
                    logger.bind(tag=TAG).info(f"Recognized speaker: {speaker_name}")

                # 转换为 JSON 字符串用于下游
                enhanced_text = json.dumps(raw_text, ensure_ascii=False)
                content_for_length_check = raw_text.get("content", "")
            else:
                # 其他 ASR 返回的纯文本
                if raw_text:
                    logger.bind(tag=TAG).info(f"Recognized text: {raw_text}")
                if speaker_name:
                    logger.bind(tag=TAG).info(f"Recognized speaker: {speaker_name}")

                # 构建包含说话人信息的JSON字符串
                enhanced_text = self._build_enhanced_text(raw_text, speaker_name)
                content_for_length_check = raw_text

            # 性能监控
            total_time = time.monotonic() - total_start_time
            conn.mark_turn_metric("asr_done")
            logger.bind(tag=TAG).info(f"Total processing time: {total_time:.3f}s")

            # 检查文本长度
            text_len, _ = remove_punctuation_and_length(content_for_length_check)
            self.stop_ws_connection()

            if text_len > 0:
                await startToChat(conn, enhanced_text)
            else:
                logger.bind(tag=TAG).warning(
                    f"ASR returned empty text for {audio_duration_ms:.0f} ms of PCM audio"
                )
                conn.complete_turn_metrics("empty_asr_result")
        except Exception as e:
            conn.complete_turn_metrics("asr_failed")
            logger.bind(tag=TAG).error(f"Failed to handle speech stop: {e}")
            import traceback

            logger.bind(tag=TAG).debug(f"Exception details: {traceback.format_exc()}")

    def _build_enhanced_text(self, text: str, speaker_name: Optional[str]) -> str:
        """构建包含说话人信息的文本（仅用于纯文本ASR）"""
        if speaker_name and speaker_name.strip():
            return json.dumps(
                {"speaker": speaker_name, "content": text}, ensure_ascii=False
            )
        else:
            return text

    def _pcm_to_wav(self, pcm_data: bytes) -> bytes:
        """将PCM数据转换为WAV格式"""
        if len(pcm_data) == 0:
            logger.bind(tag=TAG).warning("PCM data is empty; cannot convert to WAV")
            return b""

        # 确保数据长度是偶数（16位音频）
        if len(pcm_data) % 2 != 0:
            pcm_data = pcm_data[:-1]

        # 创建WAV文件头
        wav_buffer = io.BytesIO()
        try:
            with wave.open(wav_buffer, "wb") as wav_file:
                wav_file.setnchannels(1)  # 单声道
                wav_file.setsampwidth(2)  # 16位
                wav_file.setframerate(16000)  # 16kHz采样率
                wav_file.writeframes(pcm_data)

            wav_buffer.seek(0)
            wav_data = wav_buffer.read()

            return wav_data
        except Exception as e:
            logger.bind(tag=TAG).error(f"WAV conversion failed: {e}")
            return b""

    def stop_ws_connection(self):
        pass

    async def close(self):
        pass

    class AudioArtifacts(NamedTuple):
        pcm_frames: List[bytes]
        """PCM音频帧列表"""
        pcm_bytes: bytes
        """合并后的PCM音频字节数据"""
        file_path: Optional[str]
        """WAV文件路径"""
        temp_path: Optional[str]
        """临时WAV文件路径"""

    def get_current_artifacts(self) -> Optional["ASRProviderBase.AudioArtifacts"]:
        return self._current_artifacts

    def requires_file(self) -> bool:
        """是否需要文件输入"""
        return False

    def prefers_temp_file(self) -> bool:
        """是否优先使用临时文件"""
        return False

    def build_temp_file(self, pcm_bytes: bytes) -> Optional[str]:
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
                temp_path = temp_file.name
            with wave.open(temp_path, "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16000)
                wav_file.writeframes(pcm_bytes)
            return temp_path
        except Exception as e:
            logger.bind(tag=TAG).error(f"Failed to create temporary audio file: {e}")
            return None

    def save_audio_to_file(self, pcm_data: List[bytes], session_id: str) -> str:
        """PCM数据保存为WAV文件"""
        module_name = __name__.split(".")[-1]
        file_name = f"asr_{module_name}_{session_id}_{uuid.uuid4()}.wav"
        file_path = os.path.join(self.output_dir, file_name)

        with wave.open(file_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 2 bytes = 16-bit
            wf.setframerate(16000)
            wf.writeframes(b"".join(pcm_data))

        return file_path

    async def speech_to_text_wrapper(
        self, pcm_data: List[bytes], session_id: str
    ) -> Tuple[Optional[str], Optional[str]]:
        file_path = None
        temp_path = None
        try:
            combined_pcm_data = b"".join(pcm_data)

            free_space = shutil.disk_usage(self.output_dir).free
            if free_space < len(combined_pcm_data) * 2:
                raise OSError("磁盘空间不足")

            if self.requires_file() and self.prefers_temp_file():
                temp_path = self.build_temp_file(combined_pcm_data)

            if (hasattr(self, "delete_audio_file") and not self.delete_audio_file) or (
                self.requires_file() and not self.prefers_temp_file()
            ):
                file_path = self.save_audio_to_file(pcm_data, session_id)

            if len(combined_pcm_data) == 0:
                artifacts = None
            else:
                artifacts = ASRProviderBase.AudioArtifacts(
                    pcm_frames=pcm_data,
                    pcm_bytes=combined_pcm_data,
                    file_path=file_path,
                    temp_path=temp_path,
                )

            text, _ = await self.speech_to_text(
                pcm_data, session_id, artifacts
            )
            return text, file_path
        except OSError as e:
            logger.bind(tag=TAG).error(f"File operation failed: {e}")
            return None, None
        except Exception as e:
            logger.bind(tag=TAG).error(f"Speech recognition failed: {e}")
            return None, None
        finally:
            try:
                if temp_path and os.path.exists(temp_path):
                    os.unlink(temp_path)
                if (
                    hasattr(self, "delete_audio_file")
                    and self.delete_audio_file
                    and file_path
                    and os.path.exists(file_path)
                ):
                    os.remove(file_path)
            except Exception as e:
                logger.bind(tag=TAG).error(f"File cleanup failed: {e}")

    @abstractmethod
    async def speech_to_text(
        self,
        opus_data: List[bytes],
        session_id: str,
        artifacts: Optional[AudioArtifacts] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """将语音数据转换为文本

        :param opus_data: 输入的Opus音频数据
        :param session_id: 会话ID
        :param artifacts: 音频工件，包含PCM数据、文件路径等
        :return: 识别结果文本和文件路径（如果有）
        """
        pass
