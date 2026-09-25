import os
import copy
import json
import uuid
import time
import queue
import asyncio
import threading
import traceback
import websockets
import opuslib_next
import numpy as np

from core.utils.util import extract_json_from_string
from typing import Dict, Any
from collections import deque
from core.utils.modules_initialize import (
    initialize_modules,
    initialize_tts,
    initialize_asr,
)
from concurrent.futures import ThreadPoolExecutor, wait
from core.utils.dialogue import Message, Dialogue
from core.providers.asr.dto.dto import InterfaceType
from core.handle.textHandle import handleTextMessage
from core.providers.tools.unified_tool_handler import UnifiedToolHandler
from core.providers.tools.direct_answer import (
    DIRECT_ANSWER_TOOL,
    clean_response_text,
    extract_response as extract_direct_answer_response,
)
from plugins_func.loadplugins import auto_import_modules
from plugins_func.register import Action, ActionResponse
from core.auth import AuthenticationError
from core.providers.tts.dto.dto import ContentType, TTSMessageDTO, SentenceType
from config.logger import setup_logging, build_module_string, create_connection_logger
from core.utils.prompt_manager import PromptManager
from core.utils.voiceprint_provider import VoiceprintProvider
from core.utils.util import get_system_error_response, get_tool_error_response
from core.utils import text_utils
from core.utils.runtime_diagnostics import runtime_diagnostics
from core.utils.turn_diagnostics import TurnDiagnosticsMixin


TAG = __name__
_LLM_REQUEST_DUMP_LOCK = threading.Lock()

auto_import_modules("plugins_func.functions")


class TTSException(RuntimeError):
    pass


class ConnectionHandler(TurnDiagnosticsMixin):
    def __init__(
            self,
            config: Dict[str, Any],
            _vad,
            _asr,
            _llm,
            _memory,
            _intent,
    ):
        self.config = copy.deepcopy(config)
        self.session_id = str(uuid.uuid4())
        self.logger = setup_logging()
        self.websocket: websockets.ServerConnection | None = None
        self.headers = None
        self.device_id = None
        self.client_ip = None
        self.prompt = None
        self.welcome_msg = None
        self.protocol_version = 1
        self.audio_format = "opus"
        self.sample_rate = 24000  # 默认采样率，从客户端 hello 消息中动态更新

        # Client state.
        self.client_abort = False
        self.client_is_speaking = False
        self.client_listen_mode = "auto"
        self.client_aec = False  # Whether server-side AEC is enabled.

        # Worker and event-loop state.
        self.loop = None  # Assigned by handle_connection on the running loop.
        self.stop_event = threading.Event()
        self._close_lock = None
        self._close_completed = False
        self.executor = ThreadPoolExecutor(max_workers=5)
        self.chat_executor = ThreadPoolExecutor(max_workers=1)
        self.initialize_turn_diagnostics()

        # Connection-owned components.
        self.vad = None
        self.asr = None
        self.tts = None
        self._asr = _asr
        self._vad = _vad
        self.llm = _llm
        self.memory = _memory
        self.intent = _intent

        # 为每个连接单独管理声纹识别
        self.voiceprint_provider = None

        # vad相关变量
        self.client_audio_buffer = bytearray()
        self.client_have_voice = False
        self.client_voice_window = deque(maxlen=5)
        self.first_activity_time = 0.0  # 记录首次活动的时间（毫秒）
        self.last_activity_time = 0.0  # 统一的活动时间戳（毫秒）
        self.vad_last_voice_time = 0.0  # 记录用户最后一次说话的时间（毫秒）
        self.client_voice_stop = False
        self.last_is_voice = False

        # asr相关变量
        # 因为实际部署时可能会用到公共的本地ASR，不能把变量暴露给公共ASR
        # 所以涉及到ASR的变量，需要在这里定义，属于connection的私有变量
        self.asr_audio = []  # 存储PCM帧列表，供VAD和ASR共享
        self.asr_audio_queue = asyncio.Queue(
            maxsize=max(1, int(self.config.get("asr_audio_queue_max_frames", 200)))
        )
        self.asr_audio_task = None
        self.current_speaker = None  # 存储当前说话人
        self.introduced_speakers = set()  # 已"首次引入"的说话人，控制只在首轮带名字
        self.system_introduced_speakers = set()  # 已在 system 注入过身份的说话人，控制 system 身份只首轮出现

        # llm相关变量
        self.dialogue = Dialogue()
        self.active_memory_project = None

        # tts相关变量
        self.sentence_id = None
        # 处理TTS响应没有文本返回
        self.tts_MessageText = ""

        # iot相关变量
        self.iot_descriptors = {}
        self.func_handler = None
        self.pending_typed_input = None
        self.components_ready = None

        self.cmd_exit = self.config["exit_commands"]

        # 是否在聊天结束后关闭连接
        self.close_after_chat = False
        self.load_function_plugin = False
        self.intent_type = "nointent"

        self.timeout_seconds = (
                int(self.config.get("close_connection_no_voice_time", 120)) + 60
        )  # 在原来第一道关闭的基础上加60秒，进行二道关闭
        self.timeout_task = None

        # {"mcp":true} 表示启用MCP功能
        self.features = None

        # 标记连接是否来自MQTT
        self.conn_from_mqtt_gateway = False

        # 初始化提示词管理器
        self.prompt_manager = PromptManager(self.config, self.logger)

    async def handle_connection(self, ws: websockets.ServerConnection):
        try:
            # 获取运行中的事件循环（必须在异步上下文中）
            self.loop = asyncio.get_running_loop()

            # 获取并验证headers
            self.headers = dict(ws.request.headers)
            try:
                self.protocol_version = int(
                    self.headers.get("protocol-version", "1")
                )
            except (TypeError, ValueError):
                self.protocol_version = 1
            if self.protocol_version not in (1, 2, 3):
                self.logger.bind(tag=TAG).warning(
                    f"Unsupported protocol version {self.protocol_version}; falling back to version 1"
                )
                self.protocol_version = 1
            real_ip = self.headers.get("x-real-ip") or self.headers.get(
                "x-forwarded-for"
            )
            if real_ip:
                self.client_ip = real_ip.split(",")[0].strip()
            else:
                self.client_ip = ws.remote_address[0]
            self.logger.bind(tag=TAG).info(
                f"{self.client_ip} conn - Headers: {self.headers}"
            )

            self.device_id = self.headers.get("device-id", None)
            runtime_diagnostics.register_connection(
                self.session_id,
                device_id=self.device_id,
                client_ip=self.client_ip,
            )

            # 认证通过,继续处理
            self.websocket = ws

            # 检查是否来自MQTT连接
            request_path = ws.request.path
            self.conn_from_mqtt_gateway = request_path.endswith("?from=mqtt_gateway")
            if self.conn_from_mqtt_gateway:
                self.logger.bind(tag=TAG).info("Connection source: MQTT gateway")

            # 初始化活动时间戳
            self.first_activity_time = time.time() * 1000
            self.last_activity_time = time.time() * 1000

            # 启动超时检查任务
            self.timeout_task = asyncio.create_task(self._check_timeout())

            # 启动AEC缓存清理任务
            self._aec_cache_cleanup_task = asyncio.create_task(self._check_aec_cache_expiry())

            self.welcome_msg = self.config["xiaozhi"]
            self.welcome_msg["session_id"] = self.session_id

            # 从配置中读取采样率
            self.sample_rate = self.welcome_msg["audio_params"]["sample_rate"]
            self.logger.bind(tag=TAG).info(f"Output audio sample rate: {self.sample_rate}")

            # Initialize connection components without blocking the receive loop.
            self.components_ready = asyncio.Event()
            asyncio.create_task(self._background_initialize())

            try:
                async for message in self.websocket:
                    await self._route_message(message)
            except websockets.exceptions.ConnectionClosed:
                self.logger.bind(tag=TAG).info("Client disconnected")

        except AuthenticationError as e:
            self.logger.bind(tag=TAG).error(f"Authentication failed: {str(e)}")
            return
        except Exception as e:
            stack_trace = traceback.format_exc()
            self.logger.bind(tag=TAG).error(f"Connection error: {str(e)}-{stack_trace}")
            return
        finally:
            try:
                await self._save_and_close(ws)
            except Exception as final_error:
                self.logger.bind(tag=TAG).error(f"Error during final cleanup: {final_error}")
                # 确保即使保存记忆失败，也要关闭连接
                try:
                    await self.close(ws)
                except Exception as close_error:
                    self.logger.bind(tag=TAG).error(
                        f"Error while force-closing connection: {close_error}"
                    )

    async def _save_and_close(self, ws):
        """保存记忆并关闭连接"""
        try:
            if self.memory:
                # 使用线程池异步保存记忆
                def save_memory_task():
                    try:
                        # 创建新事件循环（避免与主循环冲突）
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        loop.run_until_complete(
                            self.memory.save_memory(
                                self.dialogue.dialogue, self.session_id
                            )
                        )
                    except Exception as e:
                        self.logger.bind(tag=TAG).error(f"Failed to save memory: {e}")
                    finally:
                        try:
                            loop.close()
                        except Exception:
                            pass

                # 启动线程保存记忆，不等待完成
                threading.Thread(target=save_memory_task, daemon=True).start()
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Failed to save memory: {e}")
        finally:
            # 立即关闭连接，不等待记忆保存完成
            try:
                await self.close(ws)
            except Exception as close_error:
                self.logger.bind(tag=TAG).error(
                    f"Failed to close connection after saving memory: {close_error}"
                )

    async def _route_message(self, message):
        """消息路由"""
        if self.stop_event.is_set():
            return
        if isinstance(message, str):
            await handleTextMessage(self, message)
        elif isinstance(message, bytes):
            if self.vad is None or self.asr is None:
                return

            # 处理来自MQTT网关的音频包
            if self.conn_from_mqtt_gateway and len(message) >= 16:
                handled = await self._process_mqtt_audio_message(message)
                if handled:
                    return

            if not self.conn_from_mqtt_gateway:
                message = self._unwrap_websocket_audio_message(message)
                if message is None:
                    return

            # 入口处直接解码PCM，避免VAD和ASR重复解码
            pcm_frame = self._decode_opus_packet(message)
            if pcm_frame:
                self.enqueue_asr_audio(pcm_frame)

    def _unwrap_websocket_audio_message(self, message: bytes):
        """Extract one Opus packet from the negotiated WebSocket protocol."""
        if self.protocol_version == 3:
            header_size = 4
            if len(message) < header_size:
                self.logger.bind(tag=TAG).warning("Protocol v3 audio packet is too short")
                return None
            message_type = message[0]
            payload_size = int.from_bytes(message[2:4], "big")
        elif self.protocol_version == 2:
            header_size = 16
            if len(message) < header_size:
                self.logger.bind(tag=TAG).warning("Protocol v2 audio packet is too short")
                return None
            message_type = int.from_bytes(message[2:4], "big")
            payload_size = int.from_bytes(message[12:16], "big")
        else:
            return message

        if message_type != 0:
            self.logger.bind(tag=TAG).warning(
                f"Unsupported binary message type: {message_type}"
            )
            return None
        if payload_size != len(message) - header_size:
            self.logger.bind(tag=TAG).warning(
                f"Invalid protocol v{self.protocol_version} payload size: "
                f"header={payload_size}, actual={len(message) - header_size}"
            )
            return None
        return message[header_size:]

    async def _process_mqtt_audio_message(self, message):
        """
        处理来自MQTT网关的音频消息，解析16字节头部并提取音频数据，在入队前进行AEC处理

        Args:
            message: 包含头部的音频消息

        Returns:
            bool: 是否成功处理了消息
        """
        try:
            # 解析timestamp
            timestamp = int.from_bytes(message[8:12], "big")

            audio_data = message[16:]
            # 入口直接解码PCM
            pcm_frame = self._decode_opus_packet(audio_data)
            if not pcm_frame:
                return True

            # AEC处理：如果timestamp>0且启用了AEC
            if timestamp > 0 and self.client_aec:
                pcm_frame = self._apply_aec(timestamp, pcm_frame)

            self.enqueue_asr_audio(pcm_frame)
            return True
        except RuntimeError:
            raise
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Failed to parse WebSocket audio packet: {e}")

        # 处理失败，返回False表示需要继续处理
        return False

    def _apply_aec(self, timestamp: int, pcm_frame: bytes) -> bytes:
        """应用AEC处理 - 综合算法：互相关延迟估计 + Wiener滤波 + 频谱减法"""
        try:
            if not pcm_frame or len(pcm_frame) == 0:
                return pcm_frame

            if not hasattr(self, "aec_audio_cache") or not self.aec_audio_cache:
                return pcm_frame

            mic_audio = np.frombuffer(pcm_frame, dtype=np.int16).astype(np.float32)
            mic_rms = np.sqrt(np.mean(mic_audio ** 2))

            if mic_rms < 100:
                return pcm_frame

            sorted_timestamps = sorted(self.aec_audio_cache.keys())
            if len(sorted_timestamps) < 2:
                return pcm_frame

            # ========== 匹配参考帧（对数功率谱匹配） ==========
            n = len(mic_audio)

            # 找最接近的timestamp作为起点
            closest_idx = min(range(len(sorted_timestamps)), key=lambda i: abs(sorted_timestamps[i] - timestamp))

            # 预计算 mic_audio 的对数功率谱（循环内共用，避免重复FFT）
            mic_window = np.hanning(n)
            mic_fft = np.fft.rfft(mic_audio * mic_window)
            mic_psd = np.abs(mic_fft) ** 2
            mic_log_psd = 10 * np.log10(mic_psd + 1e-8)
            mic_P_xx = np.dot(mic_log_psd, mic_log_psd)

            # 用对数功率谱匹配找最佳帧：前后各找2帧
            best_corr = -1
            best_ref_idx = closest_idx
            best_ref_rms = 0.0

            for offset in range(-2, 3):  # T-2, T-1, T, T+1, T+2
                test_idx = closest_idx + offset
                if test_idx < 0 or test_idx >= len(sorted_timestamps):
                    continue
                test_ts = sorted_timestamps[test_idx]
                test_ref = np.frombuffer(self.aec_audio_cache[test_ts], dtype=np.int16).astype(np.float32)
                test_ref_rms = np.sqrt(np.mean(test_ref ** 2))
                if test_ref_rms < 50:
                    continue

                # 对数功率谱相关性
                test_window = np.hanning(len(test_ref))
                test_fft = np.fft.rfft(test_ref * test_window)
                test_psd = np.abs(test_fft) ** 2
                test_log_psd = 10 * np.log10(test_psd + 1e-8)
                P_xy = np.dot(mic_log_psd, test_log_psd)
                P_yy = np.dot(test_log_psd, test_log_psd)
                corr = abs(P_xy) / (np.sqrt(mic_P_xx) * np.sqrt(P_yy) + 1e-8)

                if corr > best_corr:
                    best_corr = corr
                    best_ref_idx = test_idx
                    best_ref_rms = test_ref_rms

            best_ts = sorted_timestamps[best_ref_idx]
            best_ref = np.frombuffer(self.aec_audio_cache[best_ts], dtype=np.int16).astype(np.float32)
            ref_rms = best_ref_rms

            if ref_rms < 50:
                return pcm_frame

            # 对齐参考信号（直接截取相同长度）
            aligned_ref = best_ref[:n]
            if len(aligned_ref) < n:
                aligned_ref = np.pad(aligned_ref, (0, n - len(aligned_ref)))

            # ========== 频域 AEC 处理（谱减法） ==========
            # 时域信号经过声学路径后相位失真，导致时域相关性低且P_xy正负不定
            # 频域幅度谱不受相位影响，对数功率谱相关性稳定在0.97+
            # 公式：result_mag = max(|mic_fft| - |ref_fft| * scale * coef, 0)

            mic_mag = np.abs(mic_fft)
            mic_phase = np.angle(mic_fft)
            ref_fft = np.fft.rfft(aligned_ref * np.hanning(n))
            ref_mag = np.abs(ref_fft)

            # 频域计算回声比例 scale
            scale = np.sum(mic_mag * ref_mag) / (np.dot(ref_mag, ref_mag) + 1e-8)

            # 自适应系数：根据scale和coh动态调整
            # scale大（回声强）-> coef大；coh高（匹配准）-> coef大
            raw_coef = 1.0 + scale * 3 + (best_corr - 0.97) * 30
            coef = max(0.5, min(3.0, raw_coef))

            # 谱减法（过减 + 半波整流）
            echo_mag = ref_mag * scale * coef
            result_mag = np.maximum(mic_mag - echo_mag * 1.5, mic_mag * 0.1)

            # 保留相位重建信号
            result_fft = result_mag * np.exp(1j * mic_phase)
            output = np.fft.irfft(result_fft, n)

            # 高置信度是纯回声时，再压一下确保VAD检测不到
            if best_corr >= 0.97 and ref_rms > 500:
                output = output * 0.3

            # 后处理：限幅
            output = np.clip(output, -32768, 32767)

            # 转换为bytes
            result = output.astype(np.int16).tobytes()

            return result

        except Exception as e:
            self.logger.bind(tag=TAG).warning(f"[AEC] Processing failed: {e}")
            return pcm_frame

    def _decode_opus_packet(self, opus_packet: bytes) -> bytes:
        """
        解码Opus数据包为PCM数据

        Args:
            opus_packet: Opus编码的音频数据

        Returns:
            bytes: 解码后的PCM数据，失败返回None
        """
        try:
            if not opus_packet or len(opus_packet) == 0:
                return None

            self._init_connection_state(self)
            pcm_frame = self._connection_opus_decoder.decode(opus_packet, 960)
            return pcm_frame
        except Exception as e:
            self.logger.bind(tag=TAG).debug(f"Opus decoding failed: {e}")
            return None

    def _init_connection_state(self, conn):
        """为连接初始化独立的Opus解码器"""
        if not hasattr(conn, "_connection_opus_decoder"):
            conn._connection_opus_decoder = opuslib_next.Decoder(16000, 1)

    def _initialize_components(self):
        try:
            if self.tts is None:
                self.tts = self._initialize_tts()
            # 打开语音合成通道
            asyncio.run_coroutine_threadsafe(
                self.tts.open_audio_channels(self), self.loop
            )
            self.selected_module_str = build_module_string(
                self.config.get("selected_module", {})
            )
            self.logger = create_connection_logger(self.selected_module_str)

            """初始化组件"""
            if self.config.get("prompt") is not None:
                user_prompt = self.config["prompt"]
                # Render the complete template immediately so an early wake-word
                # turn follows the same policies as later turns. Dynamic context
                # is refreshed after the remaining components initialize.
                prompt = self.prompt_manager.build_enhanced_prompt(
                    user_prompt,
                    self.device_id,
                    emoji_enabled=(self.features or {}).get("emoji", True),
                )
                if not prompt:
                    prompt = self.prompt_manager.get_quick_prompt(user_prompt)
                self.change_system_prompt(prompt)
                self.logger.bind(tag=TAG).info(
                    f"Fast component initialization: prompt loaded successfully: {prompt[:50]}..."
                )

            """初始化本地组件"""
            if self.vad is None:
                self.vad = self._vad
            if self.asr is None:
                self.asr = self._initialize_asr()

            # 初始化声纹识别
            self._initialize_voiceprint()
            # 打开语音识别通道
            asyncio.run_coroutine_threadsafe(
                self.asr.open_audio_channels(self), self.loop
            )

            """加载记忆"""
            self._initialize_memory()
            """加载意图识别"""
            self._initialize_intent()
            """更新系统提示词"""
            self._init_prompt_enhancement()
            """注入工具调用few-shot示例（仅function_call模式）"""
            self._inject_tool_call_fewshot()
            return True

        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Failed to instantiate components: {e}")
            return False

    def _init_prompt_enhancement(self):

        # 更新上下文信息
        self.prompt_manager.update_context_info(self, self.client_ip)
        enhanced_prompt = self.prompt_manager.build_enhanced_prompt(
            self.config["prompt"],
            self.device_id,
            self.client_ip,
            emoji_enabled=(self.features or {}).get("emoji", True),
        )
        if enhanced_prompt:
            self.change_system_prompt(enhanced_prompt)
            self.logger.bind(tag=TAG).debug("System prompt enhanced and updated")

    def _inject_tool_call_fewshot(self):
        """注入工具调用 few-shot 示例到对话历史。
        结构：正样本（工具调用示例）放在动态 system 之前，可命中前缀缓存；
        负样本（直接回答示例）放在动态 system 之后、紧挨真实用户消息，
        确保模型在处理用户消息前最后看到的是"不调工具"的行为模式。
        """
        if self.intent_type != "function_call":
            return
        if not hasattr(self, "func_handler") or self.func_handler is None:
            return

        tools = self.func_handler.get_functions()
        if not tools:
            return

        tool_names = {t.get("function", {}).get("name") for t in tools}

        # === few-shot 示例（is_temporary）===
        # 展示 direct_answer 携带 response 参数的用法，一次调用完成回复

        if (
            self.config.get("enable_direct_answer_tool", True)
            and getattr(self.llm, "supports_direct_answer_tool", True)
        ):
            # Example 1: direct_answer returns its response without another LLM pass.
            da_tc_id = "fewshot_da_001"
            self.dialogue.put(Message(role="user", content="What is 2 + 2?", is_temporary=True))
            self.dialogue.put(Message(
                role="assistant",
                tool_calls=[{
                    "id": da_tc_id,
                    "function": {"arguments": '{"response": "4."}', "name": "direct_answer"},
                    "type": "function", "index": 0,
                }],
                is_temporary=True,
            ))
            self.dialogue.put(Message(
                role="tool", tool_call_id=da_tc_id,
                content="Response sent", is_temporary=True,
            ))

        # Example 2: a real tool call (handle_exit_intent).
        if "handle_exit_intent" in tool_names:
            tc_id = "fewshot_exit_001"
            self.dialogue.put(Message(role="user", content="Goodbye", is_temporary=True))
            self.dialogue.put(Message(
                role="assistant",
                tool_calls=[{
                    "id": tc_id,
                    "function": {"arguments": "{}", "name": "handle_exit_intent"},
                    "type": "function", "index": 0,
                }],
                is_temporary=True,
            ))
            self.dialogue.put(Message(
                role="tool", tool_call_id=tc_id,
                content="Exit intent handled", is_temporary=True,
            ))
            self.dialogue.put(Message(
                role="assistant", content="Goodbye", is_temporary=True,
            ))

        self.logger.bind(tag=TAG).debug("Tool-call few-shot examples injected")

    def _initialize_tts(self):
        """Initialize TTS."""
        return initialize_tts(self.config)

    def _initialize_asr(self):
        """初始化ASR"""
        if (
                self._asr is not None
                and hasattr(self._asr, "interface_type")
                and self._asr.interface_type == InterfaceType.LOCAL
        ):
            # 如果公共ASR是本地服务，则直接返回
            # 因为本地一个实例ASR，可以被多个连接共享
            asr = self._asr
        else:
            # 如果公共ASR是远程服务，则初始化一个新实例
            # 因为远程ASR，涉及到websocket连接和接收线程，需要每个连接一个实例
            asr = initialize_asr(self.config)

        return asr

    def _initialize_voiceprint(self):
        """为当前连接初始化声纹识别"""
        try:
            voiceprint_config = self.config.get("voiceprint", {})
            if voiceprint_config:
                voiceprint_provider = VoiceprintProvider(voiceprint_config)
                if voiceprint_provider is not None and voiceprint_provider.enabled:
                    self.voiceprint_provider = voiceprint_provider
                    self.logger.bind(tag=TAG).info("Voiceprint recognition enabled dynamically for this connection")
                else:
                    self.logger.bind(tag=TAG).warning("Voiceprint recognition is enabled but configuration is incomplete")
            else:
                self.logger.bind(tag=TAG).info("Voiceprint recognition is disabled")
        except Exception as e:
            self.logger.bind(tag=TAG).warning(f"Failed to initialize voiceprint recognition: {str(e)}")

    async def _background_initialize(self):
        """Initialize connection-scoped components in the worker pool."""
        try:
            initialized = await self.loop.run_in_executor(
                self.executor, self._initialize_components
            )
            if not initialized:
                return
            self.components_ready.set()

            from core.handle.receiveAudioHandle import (
                process_pending_typed_input_if_ready,
            )

            await process_pending_typed_input_if_ready(self)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Background initialization failed: {e}")

    def _initialize_memory(self):
        if self.memory is None:
            return
        """初始化记忆模块"""
        self.memory.init_memory(
            role_id=self.device_id,
            llm=self.llm,
            summary_memory=self.config.get("summaryMemory", None),
        )

        # 获取记忆总结配置
        memory_config = self.config["Memory"]
        memory_type = self.config["Memory"][self.config["selected_module"]["Memory"]][
            "type"
        ]
        # No-memory mode needs no additional LLM wiring.
        if memory_type == "nomem":
            return
        # 使用 mem_local_short 模式
        elif memory_type == "mem_local_short":
            memory_llm_name = memory_config[self.config["selected_module"]["Memory"]][
                "llm"
            ]
            if memory_llm_name and memory_llm_name in self.config["LLM"]:
                # 如果配置了专用LLM，则创建独立的LLM实例
                from core.utils import llm as llm_utils

                memory_llm_config = self.config["LLM"][memory_llm_name]
                memory_llm_type = memory_llm_config.get("type", memory_llm_name)
                memory_llm = llm_utils.create_instance(
                    memory_llm_type, memory_llm_config
                )
                self.logger.bind(tag=TAG).info(
                    f"Created dedicated LLM for memory summarization: {memory_llm_name}, type: {memory_llm_type}"
                )
                self.memory.set_llm(memory_llm)
            else:
                # 否则使用主LLM
                self.memory.set_llm(self.llm)
                self.logger.bind(tag=TAG).info("Using the primary LLM for intent recognition")

    def _initialize_intent(self):
        if self.intent is None:
            return
        self.intent_type = self.config["Intent"][
            self.config["selected_module"]["Intent"]
        ]["type"]
        if self.intent_type == "function_call" or self.intent_type == "intent_llm":
            self.load_function_plugin = True
        """初始化意图识别模块"""
        # 获取意图识别配置
        intent_config = self.config["Intent"]
        intent_type = self.config["Intent"][self.config["selected_module"]["Intent"]][
            "type"
        ]

        # 如果使用 nointent，直接返回
        if intent_type == "nointent":
            return
        # 使用 intent_llm 模式
        elif intent_type == "intent_llm":
            intent_llm_name = intent_config[self.config["selected_module"]["Intent"]][
                "llm"
            ]

            if intent_llm_name and intent_llm_name in self.config["LLM"]:
                # 如果配置了专用LLM，则创建独立的LLM实例
                from core.utils import llm as llm_utils

                intent_llm_config = self.config["LLM"][intent_llm_name]
                intent_llm_type = intent_llm_config.get("type", intent_llm_name)
                intent_llm = llm_utils.create_instance(
                    intent_llm_type, intent_llm_config
                )
                self.logger.bind(tag=TAG).info(
                    f"Created dedicated LLM for intent recognition: {intent_llm_name}, type: {intent_llm_type}"
                )
                self.intent.set_llm(intent_llm)
            else:
                # 否则使用主LLM
                self.intent.set_llm(self.llm)
                self.logger.bind(tag=TAG).info("Using the primary LLM for intent recognition")

        """加载统一工具处理器"""
        self.func_handler = UnifiedToolHandler(self)

        # 异步初始化工具处理器
        if hasattr(self, "loop") and self.loop:
            asyncio.run_coroutine_threadsafe(self.func_handler._initialize(), self.loop)

    def change_system_prompt(self, prompt):
        self.prompt = prompt
        # 更新系统prompt至上下文
        self.dialogue.update_system_message(self.prompt)

    def _dump_full_llm_request(self, dialogue, functions, depth):
        dump_enabled = self.config.get(
            "dump_full_llm_request",
            self.config.get("log_full_llm_request", False),
        )
        if not dump_enabled:
            return

        provider_name = self.config.get("selected_module", {}).get("LLM")
        provider_config = self.config.get("LLM", {}).get(provider_name, {})
        request_payload = {
            "timestamp": time.time(),
            "provider": provider_name,
            "model": provider_config.get("model_name"),
            "session_id": self.session_id,
            "sentence_id": self.sentence_id,
            "tool_call_depth": depth,
            "messages": dialogue,
            "tools": functions,
        }
        dump_path = str(
            self.config.get("llm_request_dump_file", "tmp/llm_requests.jsonl")
        ).strip()
        if not dump_path:
            dump_path = "tmp/llm_requests.jsonl"

        try:
            dump_dir = os.path.dirname(dump_path)
            if dump_dir:
                os.makedirs(dump_dir, exist_ok=True)
            serialized_request = json.dumps(
                request_payload,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
            with _LLM_REQUEST_DUMP_LOCK:
                with open(dump_path, "a", encoding="utf-8") as dump_file:
                    dump_file.write(serialized_request + "\n")
        except OSError as error:
            self.logger.bind(tag=TAG).error(
                f"Failed to dump full LLM request to {dump_path}: {error}"
            )

    def chat(self, query, depth=0, memory_str=None):
        if depth == 0:
            self.client_abort = False
        return self._chat(query, depth, memory_str)

    def _build_memory_recall_context(self, query):
        recent_messages = [
            message.content
            for message in self.dialogue.dialogue
            if message.role in {"user", "assistant"}
            and not message.is_temporary
            and isinstance(message.content, str)
            and message.content.strip()
        ]
        if recent_messages and recent_messages[-1] == query:
            recent_messages.pop()
        recent_messages = recent_messages[-6:]

        resolve_project = getattr(self.memory, "resolve_active_project", None)
        if callable(resolve_project):
            self.active_memory_project = resolve_project(
                query,
                recent_messages=recent_messages,
                current=self.active_memory_project,
            )
        return {
            "recent_messages": recent_messages,
            "active_project": self.active_memory_project,
        }

    def _chat(self, query, depth=0, memory_str=None):
        # Keep the sentence ID local so a newer turn cannot overwrite it.
        current_sentence_id = None
        llm_started_at = time.monotonic()

        if query is not None:
            self.logger.bind(tag=TAG).info(f"LLM received user message: {query}")

        # A top-level request owns a new sentence and TTS start marker.
        if depth == 0:
            current_sentence_id = str(uuid.uuid4().hex)
            self.sentence_id = current_sentence_id
            self.record_turn_input(query or "")
            self.mark_turn_metric("llm_dispatch", sentence_id=current_sentence_id)
            self.dialogue.put(Message(role="user", content=query))
            self.tts.tts_text_queue.put(
                TTSMessageDTO(
                    sentence_id=current_sentence_id,
                    sentence_type=SentenceType.FIRST,
                    content_type=ContentType.ACTION,
                )
            )
        else:
            # Tool continuations stay attached to the current sentence.
            current_sentence_id = self.sentence_id

        # Bound recursive tool continuations to prevent loops.
        MAX_DEPTH = 5
        force_final_answer = False

        if depth >= MAX_DEPTH:
            self.logger.bind(tag=TAG).debug(
                f"Maximum tool-call depth of {MAX_DEPTH} reached; forcing an answer from available information"
            )
            force_final_answer = True
            # Ask for a final answer using only the evidence already collected.
            self.dialogue.put(
                Message(
                    role="user",
                    content="The tool-call limit has been reached. Answer directly using the information already available and do not call another tool.",
                )
            )

        # Define intent functions
        functions = None
        # Disable tools at the depth limit so the model must answer directly.
        if (
                self.intent_type == "function_call"
                and hasattr(self, "func_handler")
                and not force_final_answer
        ):
            functions = list(self.func_handler.get_functions())
            # Offer direct_answer only on the first request. Excluding it from
            # continuations prevents a second synthetic call from looping.
            if (
                functions is not None
                and depth == 0
                and self.config.get("enable_direct_answer_tool", True)
                and getattr(self.llm, "supports_direct_answer_tool", True)
            ):
                functions.append(DIRECT_ANSWER_TOOL)

        response_message = []

        try:
            self.mark_turn_metric(
                "llm_request" if depth == 0 else "resumed_llm_request"
            )
            # Query memory once for the user turn, then preserve the same
            # evidence across any LLM continuations after tool results.
            if memory_str is None and self.memory is not None and query:
                memory_context = self._build_memory_recall_context(query)
                future = asyncio.run_coroutine_threadsafe(
                    self.memory.query_memory(query, context=memory_context), self.loop
                )
                memory_str = future.result()

            # Inject speaker identity into the system context only once. The
            # dialogue history retains it without encouraging repeated names.
            speaker_for_system = None
            cs = (self.current_speaker or "").strip()
            if cs and cs != "未知说话人" and cs not in self.system_introduced_speakers:
                self.system_introduced_speakers.add(cs)
                speaker_for_system = cs

            llm_dialogue = self.dialogue.get_llm_dialogue_with_memory(
                memory_str, self.config.get("voiceprint", {}), speaker_for_system
            )
            self._dump_full_llm_request(llm_dialogue, functions, depth)
            provider_kwargs = {}
            if getattr(self.llm, "supports_request_cancellation", False):
                provider_kwargs["event_loop"] = self.loop

            if self.intent_type == "function_call" and functions is not None:
                # Use the provider's streaming function-call interface.
                llm_responses = self.llm.response_with_functions(
                    self.session_id,
                    llm_dialogue,
                    functions=functions,
                    **provider_kwargs,
                )
            else:
                llm_responses = self.llm.response(
                    self.session_id,
                    llm_dialogue,
                    **provider_kwargs,
                )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"LLM processing failed for {query}: {e}")
            self.tts.tts_text_queue.put(
                TTSMessageDTO(
                    sentence_id=current_sentence_id,
                    sentence_type=SentenceType.MIDDLE,
                    content_type=ContentType.TEXT,
                    content_detail=get_system_error_response(self.config),
                )
            )
            if depth == 0:
                self.tts.tts_text_queue.put(
                    TTSMessageDTO(
                        sentence_id=current_sentence_id,
                        sentence_type=SentenceType.LAST,
                        content_type=ContentType.ACTION,
                    )
                )
            return None

        # Consume the streaming response.
        tool_call_flag = False
        # Accumulate one or more tool calls emitted by the provider.
        tool_calls_list = []
        content_arguments = ""
        emotion_flag = True
        try:
            for response_index, response in enumerate(llm_responses):
                if self.client_abort:
                    break
                if depth == 0 and response_index == 0:
                    self.mark_turn_metric("llm_first_response")
                    self.logger.bind(tag=TAG).info(
                        f"LLM first response received after {time.monotonic() - llm_started_at:.3f}s"
                    )
                elif depth > 0 and response_index == 0:
                    self.mark_turn_metric("resumed_llm_first_response")
                if self.intent_type == "function_call" and functions is not None:
                    content, tools_call = response
                    if "content" in response:
                        content = response["content"]
                        tools_call = None
                    if content is not None and len(content) > 0:
                        content_arguments += content

                    if not tool_call_flag and content_arguments.startswith("<tool_call>"):
                        # print("content_arguments", content_arguments)
                        tool_call_flag = True

                    if tools_call is not None and len(tools_call) > 0:
                        tool_call_flag = True
                        self._merge_tool_calls(tool_calls_list, tools_call)

                    # Stream direct_answer text to TTS while retaining a short
                    # buffer so trailing JSON syntax cannot leak into speech.
                    _DA_STREAM_BUFFER = 5
                    for tc in tool_calls_list:
                        if tc["name"] == "direct_answer" and tc.get("arguments"):
                            da_text = extract_direct_answer_response(tc["arguments"])
                            sent_len = tc.get("_da_sent", 0)
                            if da_text and len(da_text) > sent_len:
                                safe_end = max(sent_len, len(da_text) - _DA_STREAM_BUFFER)
                                if safe_end > sent_len:
                                    new_part = da_text[sent_len:safe_end]
                                    # Remove any trailing JSON syntax from the delta.
                                    new_part = clean_response_text(new_part)
                                    if new_part:
                                        tc["_da_sent"] = safe_end
                                        self.tts.tts_text_queue.put(
                                            TTSMessageDTO(
                                                sentence_id=current_sentence_id,
                                                sentence_type=SentenceType.MIDDLE,
                                                content_type=ContentType.TEXT,
                                                content_detail=new_part,
                                            )
                                        )
                else:
                    content = response

                # Derive the display emotion once from the start of the reply.
                if emotion_flag and content is not None and content.strip():
                    if (self.features or {}).get("emoji", True):
                        asyncio.run_coroutine_threadsafe(
                            text_utils.send_emotion_message(self, content),
                            self.loop,
                        )
                    emotion_flag = False

                if content is not None and len(content) > 0:
                    if not tool_call_flag:
                        response_message.append(content)
                        self.tts.tts_text_queue.put(
                            TTSMessageDTO(
                                sentence_id=current_sentence_id,
                                sentence_type=SentenceType.MIDDLE,
                                content_type=ContentType.TEXT,
                                content_detail=content,
                            )
                        )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"LLM stream processing error: {e}")
            self.tts.tts_text_queue.put(
                TTSMessageDTO(
                    sentence_id=current_sentence_id,
                    sentence_type=SentenceType.MIDDLE,
                    content_type=ContentType.TEXT,
                    content_detail=get_system_error_response(self.config),
                )
            )
            if depth == 0:
                self.tts.tts_text_queue.put(
                    TTSMessageDTO(
                        sentence_id=current_sentence_id,
                        sentence_type=SentenceType.LAST,
                        content_type=ContentType.ACTION,
                    )
                )
            return

        # The abort path already stopped TTS and cleared the display status.
        # Do not enqueue stale completion markers or execute tool calls after
        # cancelling the provider request.
        if self.client_abort:
            return None

        # Execute any function calls emitted by the model.
        if tool_call_flag:
            bHasError = False
            # Parse providers that encode tool calls inside text.
            if len(tool_calls_list) == 0 and content_arguments:
                a = extract_json_from_string(content_arguments)
                if a is not None:
                    try:
                        content_arguments_json = json.loads(a)
                        tool_calls_list.append(
                            {
                                "id": str(uuid.uuid4().hex),
                                "name": content_arguments_json["name"],
                                "arguments": json.dumps(
                                    content_arguments_json["arguments"],
                                    ensure_ascii=False,
                                ),
                            }
                        )
                    except Exception as e:
                        bHasError = True
                        response_message.append(a)
                else:
                    bHasError = True
                    response_message.append(content_arguments)
                if bHasError:
                    self.logger.bind(tag=TAG).error(
                        f"function call error: {content_arguments}"
                    )

            if not bHasError and len(tool_calls_list) > 0:
                # Handle the synthetic direct_answer tool separately.
                direct_answer_calls = [tc for tc in tool_calls_list if tc["name"] == "direct_answer"]
                real_tool_calls = [tc for tc in tool_calls_list if tc["name"] != "direct_answer"]

                if direct_answer_calls:
                    self.logger.bind(tag=TAG).debug(
                        "Model selected direct_answer; streamed response was played and added to dialogue history"
                    )
                    has_direct_answer_output = False
                    for tc in direct_answer_calls:
                        da_response = clean_response_text(
                            extract_direct_answer_response(
                                tc.get("arguments", "{}")
                            )
                        )
                        if da_response:
                            has_direct_answer_output = True
                            self.append_turn_output(da_response)
                            # Flush text retained by the streaming safety buffer.
                            sent_len = tc.get("_da_sent", 0)
                            remaining = da_response[sent_len:]
                            if remaining:
                                self.tts.tts_text_queue.put(
                                    TTSMessageDTO(
                                        sentence_id=current_sentence_id,
                                        sentence_type=SentenceType.MIDDLE,
                                        content_type=ContentType.TEXT,
                                        content_detail=remaining,
                                    )
                                )
                            # Preserve the final response in dialogue history.
                            self.tts.store_tts_text(current_sentence_id, da_response)
                            self.dialogue.put(Message(role="assistant", content=da_response))

                    if not real_tool_calls:
                        if not has_direct_answer_output:
                            self.logger.bind(tag=TAG).warning(
                                "Model returned direct_answer without a usable response"
                            )
                            self.tts.tts_text_queue.put(
                                TTSMessageDTO(
                                    sentence_id=current_sentence_id,
                                    sentence_type=SentenceType.MIDDLE,
                                    content_type=ContentType.TEXT,
                                    content_detail=get_system_error_response(self.config),
                                )
                            )
                        if depth == 0:
                            self.tts.tts_text_queue.put(
                                TTSMessageDTO(
                                    sentence_id=current_sentence_id,
                                    sentence_type=SentenceType.LAST,
                                    content_type=ContentType.ACTION,
                                )
                            )
                        return

                    tool_calls_list = real_tool_calls

            if not bHasError and len(tool_calls_list) > 0:
                self.logger.bind(tag=TAG).debug(
                    f"Detected {len(tool_calls_list)} tool call(s)"
                )

                # Preserve any text spoken before the tool call completed.
                streamed_text = ""
                if len(response_message) > 0:
                    streamed_text = "".join(response_message)
                    self.append_turn_output(streamed_text)
                    self.tts.store_tts_text(current_sentence_id, streamed_text)
                    self.dialogue.put(Message(role="assistant", content=streamed_text))
                response_message.clear()

                # Dispatch all calls before waiting so independent tools can overlap.
                futures_with_data = []
                for tool_call_data in tool_calls_list:
                    self.logger.bind(tag=TAG).debug(
                        f"function_name={tool_call_data['name']}, function_id={tool_call_data['id']}, function_arguments={tool_call_data['arguments']}"
                    )

                    tool_type = self.func_handler.tool_manager.get_tool_type(
                        tool_call_data["name"]
                    )
                    self.start_tool_metric(
                        tool_call_data["id"],
                        tool_call_data["name"],
                        arguments=tool_call_data.get("arguments"),
                        tool_type=(tool_type.value if tool_type else None),
                    )
                    future = asyncio.run_coroutine_threadsafe(
                        self.observe_tool_call(
                            tool_call_data["id"],
                            self.func_handler.handle_llm_function_call(
                                self, tool_call_data
                            ),
                        ),
                        self.loop,
                    )
                    futures_with_data.append((future, tool_call_data))

                # Apply the configured timeout to the complete tool batch.
                tool_call_timeout = int(self.config.get("tool_call_timeout", 30))
                tool_results = []
                completed_futures, _ = wait(
                    [future for future, _ in futures_with_data],
                    timeout=tool_call_timeout,
                )

                for future, tool_call_data in futures_with_data:
                    if future not in completed_futures:
                        self.finish_tool_metric(
                            tool_call_data["id"],
                            "timed_out",
                            result="Tool call timed out",
                        )
                        future.cancel()
                        self.logger.bind(tag=TAG).error(
                            f"Tool call timed out: {tool_call_data['name']}"
                        )
                        tool_results.append((
                            ActionResponse(
                                action=Action.ERROR,
                                result="Tool call timed out",
                                response=get_tool_error_response(
                                    self.config, timed_out=True
                                ),
                            ),
                            tool_call_data,
                        ))
                        continue
                    try:
                        result = future.result()
                        tool_results.append((result, tool_call_data))
                    except Exception as e:
                        self.logger.bind(tag=TAG).error(
                            f"Tool call failed: {tool_call_data['name']}, error: {e}"
                        )
                        # Convert failures into a bounded user-facing tool error.
                        tool_results.append((
                            ActionResponse(
                                action=Action.ERROR,
                                result=str(e),
                                response=get_tool_error_response(self.config),
                            ),
                            tool_call_data
                        ))
                # Apply all results through the same continuation path.
                if tool_results:
                    self._handle_function_result(
                        tool_results,
                        depth=depth,
                        streamed_text=streamed_text,
                        memory_str=memory_str,
                    )

        # Store direct model output in the dialogue.
        if len(response_message) > 0:
            text_buff = "".join(response_message)
            self.append_turn_output(text_buff)
            self.tts.store_tts_text(current_sentence_id, text_buff)
            self.dialogue.put(Message(role="assistant", content=text_buff))

        if depth == 0:
            self.tts.tts_text_queue.put(
                TTSMessageDTO(
                    sentence_id=current_sentence_id,
                    sentence_type=SentenceType.LAST,
                    content_type=ContentType.ACTION,
                )
            )
            # Build the verbose dialogue dump only when debug logging consumes it.
            self.logger.bind(tag=TAG).debug(
                lambda: json.dumps(
                    self.dialogue.get_llm_dialogue(), indent=4, ensure_ascii=False
                )
            )

        return True

    def _handle_function_result(
        self, tool_results, depth, streamed_text="", memory_str=None
    ):
        need_llm_tools = []
        record_tools = []

        for result, tool_call_data in tool_results:
            if result.action in [
                Action.RESPONSE,
                Action.NOTFOUND,
                Action.ERROR,
            ]:
                text = result.response if result.response else result.result
                if streamed_text and text in streamed_text:
                    self.logger.bind(tag=TAG).debug(
                        f"Skipping duplicate TTS for tool {tool_call_data['name']}, already streamed"
                    )
                else:
                    self.tts.tts_one_sentence(self, ContentType.TEXT, content_detail=text)
                    self.tts.store_tts_text(self.sentence_id, text)
                self.append_turn_output(text)
                self.dialogue.put(Message(role="assistant", content=text))
            elif result.action == Action.REQLLM:
                need_llm_tools.append((result, tool_call_data))
            elif result.action == Action.RECORD:
                record_tools.append((result, tool_call_data))
            else:
                pass

        # RECORD writes the complete tool chain without another LLM request.
        if record_tools:
            # Record which calls the assistant selected.
            all_tool_calls = [
                {
                    "id": tool_call_data["id"],
                    "function": {
                        "arguments": (
                            "{}"
                            if tool_call_data["arguments"] == ""
                            else tool_call_data["arguments"]
                        ),
                        "name": tool_call_data["name"],
                    },
                    "type": "function",
                    "index": idx,
                }
                for idx, (_, tool_call_data) in enumerate(record_tools)
            ]
            self.dialogue.put(Message(role="assistant", tool_calls=all_tool_calls))

            # Record each tool result for future conversational context.
            for result, tool_call_data in record_tools:
                text = result.result or ""
                self.dialogue.put(
                    Message(
                        role="tool",
                        tool_call_id=(
                            str(uuid.uuid4())
                            if tool_call_data["id"] is None
                            else tool_call_data["id"]
                        ),
                        content=text,
                    )
                )

            # Add a final assistant message so the next user message never
            # follows a tool result directly.
            response_parts = []
            for result, _ in record_tools:
                resp = result.response or result.result
                if resp:
                    response_parts.append(resp)
            if response_parts:
                recorded_response = ", ".join(response_parts)
                self.append_turn_output(recorded_response)
                self.dialogue.put(
                    Message(role="assistant", content=recorded_response)
                )

        if need_llm_tools:
            all_tool_calls = [
                {
                    "id": tool_call_data["id"],
                    "function": {
                        "arguments": (
                            "{}"
                            if tool_call_data["arguments"] == ""
                            else tool_call_data["arguments"]
                        ),
                        "name": tool_call_data["name"],
                    },
                    "type": "function",
                    "index": idx,
                }
                for idx, (_, tool_call_data) in enumerate(need_llm_tools)
            ]
            self.dialogue.put(Message(role="assistant", tool_calls=all_tool_calls))

            for result, tool_call_data in need_llm_tools:
                text = result.result
                if text is not None and len(text) > 0:
                    self.dialogue.put(
                        Message(
                            role="tool",
                            tool_call_id=(
                                str(uuid.uuid4())
                                if tool_call_data["id"] is None
                                else tool_call_data["id"]
                            ),
                            content=text,
                        )
                    )

            self.chat(None, depth=depth + 1, memory_str=memory_str)

    def clearSpeakStatus(self):
        self.client_is_speaking = False
        self.logger.bind(tag=TAG).debug("Cleared server speaking state")

    def cancel_active_llm(self):
        """Cancel this connection's provider request when supported."""
        cancel = getattr(self.llm, "cancel", None)
        if not callable(cancel):
            return False
        try:
            return bool(cancel(self.session_id))
        except Exception as error:
            self.logger.bind(tag=TAG).warning(
                f"Failed to cancel active LLM request: {error}"
            )
            return False

    async def close(self, ws=None):
        """Release connection resources once, even when close paths race."""
        if self._close_completed:
            return
        if self.stop_event:
            self.stop_event.set()
        if self._close_lock is None:
            self._close_lock = asyncio.Lock()

        async with self._close_lock:
            if self._close_completed:
                return
            await self._close_resources(ws)
            self._close_completed = True

    async def _close_resources(self, ws=None):
        """资源清理方法"""
        try:
            self.complete_turn_metrics("connection_closed")
            self.cancel_active_llm()
            # 清理 VAD 连接资源
            if (
                    hasattr(self, "vad")
                    and self.vad
                    and hasattr(self.vad, "release_conn_resources")
            ):
                self.vad.release_conn_resources(self)

            # 清理opus解码器
            if hasattr(self, "_connection_opus_decoder"):
                try:
                    delattr(self, "_connection_opus_decoder")
                except Exception:
                    pass

            # 清理音频缓冲区
            if hasattr(self, "audio_buffer"):
                self.audio_buffer.clear()

            # 取消超时任务
            if self.timeout_task and not self.timeout_task.done():
                if self.timeout_task is not asyncio.current_task():
                    self.timeout_task.cancel()
                    try:
                        await self.timeout_task
                    except asyncio.CancelledError:
                        pass
                self.timeout_task = None

            if self.asr_audio_task and not self.asr_audio_task.done():
                if self.asr_audio_task is not asyncio.current_task():
                    self.asr_audio_task.cancel()
                    try:
                        await self.asr_audio_task
                    except asyncio.CancelledError:
                        pass
                self.asr_audio_task = None

            # 取消AEC缓存清理任务
            if hasattr(self, "_aec_cache_cleanup_task") and self._aec_cache_cleanup_task and not self._aec_cache_cleanup_task.done():
                self._aec_cache_cleanup_task.cancel()
                try:
                    await self._aec_cache_cleanup_task
                except asyncio.CancelledError:
                    pass
                self._aec_cache_cleanup_task = None

            # 清理AEC缓存
            if hasattr(self, "aec_audio_cache"):
                self.aec_audio_cache.clear()
                self.aec_audio_cache_time.clear()

            # 清理工具处理器资源
            if hasattr(self, "func_handler") and self.func_handler:
                try:
                    await self.func_handler.cleanup()
                except Exception as cleanup_error:
                    self.logger.bind(tag=TAG).error(
                        f"Error cleaning up tool handler: {cleanup_error}"
                    )

            # 触发停止事件
            if self.stop_event:
                self.stop_event.set()

            # 清空任务队列
            self.clear_queues()

            # 关闭WebSocket连接
            try:
                if ws:
                    # 安全地检查WebSocket状态并关闭
                    try:
                        if hasattr(ws, "closed") and not ws.closed:
                            await ws.close()
                        elif hasattr(ws, "state") and ws.state.name != "CLOSED":
                            await ws.close()
                        else:
                            # 如果没有closed属性，直接尝试关闭
                            await ws.close()
                    except Exception:
                        # 如果关闭失败，忽略错误
                        pass
                elif self.websocket:
                    try:
                        if (
                                hasattr(self.websocket, "closed")
                                and not self.websocket.closed
                        ):
                            await self.websocket.close()
                        elif (
                                hasattr(self.websocket, "state")
                                and self.websocket.state.name != "CLOSED"
                        ):
                            await self.websocket.close()
                        else:
                            # 如果没有closed属性，直接尝试关闭
                            await self.websocket.close()
                    except Exception:
                        # 如果关闭失败，忽略错误
                        pass
            except Exception as ws_error:
                self.logger.bind(tag=TAG).error(f"Error closing WebSocket connection: {ws_error}")

            if self.tts:
                await self.tts.close()
            if self.asr:
                await self.asr.close()

            # 最后关闭线程池（避免阻塞）
            if self.executor:
                try:
                    self.executor.shutdown(wait=False)
                except Exception as executor_error:
                    self.logger.bind(tag=TAG).error(
                        f"Error shutting down thread pool: {executor_error}"
                    )
                self.executor = None
            if self.chat_executor:
                try:
                    self.chat_executor.shutdown(wait=False)
                except Exception as executor_error:
                    self.logger.bind(tag=TAG).error(
                        f"Error shutting down chat thread pool: {executor_error}"
                    )
                self.chat_executor = None
            self.logger.bind(tag=TAG).info("Connection resources released")
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Error closing connection: {e}")
        finally:
            # 确保停止事件被设置
            if self.stop_event:
                self.stop_event.set()
            runtime_diagnostics.unregister_connection(self.session_id)

    def clear_queues(self):
        """清空所有任务队列"""
        if self.tts:
            self.logger.bind(tag=TAG).debug(
                f"Cleanup started: TTS queue size={self.tts.tts_text_queue.qsize()}, audio queue size={self.tts.tts_audio_queue.qsize()}"
            )

            # 使用非阻塞方式清空队列
            for q in [
                self.tts.tts_text_queue,
                self.tts.tts_audio_queue,
            ]:
                if not q:
                    continue
                while True:
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        break

            # Reset the sender even when the decoded input queue is empty.
            if hasattr(self, "audio_rate_controller") and self.audio_rate_controller:
                self.audio_rate_controller.reset()
                self.logger.bind(tag=TAG).debug("Audio rate controller reset")

            self.logger.bind(tag=TAG).debug(
                f"Cleanup finished: TTS queue size={self.tts.tts_text_queue.qsize()}, audio queue size={self.tts.tts_audio_queue.qsize()}"
            )

        while True:
            try:
                self.asr_audio_queue.get_nowait()
                self.asr_audio_queue.task_done()
            except asyncio.QueueEmpty:
                break

    def reset_audio_states(self):
        """
        重置所有音频相关状态(VAD + ASR)
        """
        # Reset VAD states
        self.client_audio_buffer.clear()
        self.client_have_voice = False
        self.client_voice_stop = False
        self.client_voice_window.clear()
        self.last_is_voice = False
        self.vad_last_voice_time = 0.0

        # Clear ASR buffers
        self.asr_audio.clear()

        self.logger.bind(tag=TAG).debug("All audio states reset.")

    def chat_and_close(self, text):
        """Chat with the user and then close the connection"""
        try:
            # Use the existing chat method
            self.chat(text)

            # After chat is complete, close the connection
            self.close_after_chat = True
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Chat and close error: {str(e)}")

    async def _check_timeout(self):
        """检查连接超时"""
        try:
            while not self.stop_event.is_set():
                last_activity_time = self.last_activity_time

                # 检查是否超时（只有在时间戳已初始化的情况下）
                if last_activity_time > 0.0:
                    current_time = time.time() * 1000
                    if current_time - last_activity_time > self.timeout_seconds * 1000:
                        if not self.stop_event.is_set():
                            self.logger.bind(tag=TAG).info("Connection timed out; preparing to close")
                            # 设置停止事件，防止重复处理
                            self.stop_event.set()
                            # 使用 try-except 包装关闭操作，确保不会因为异常而阻塞
                            try:
                                await self.close(self.websocket)
                            except Exception as close_error:
                                self.logger.bind(tag=TAG).error(
                                    f"Error closing timed-out connection: {close_error}"
                                )
                        break
                # 每10秒检查一次，避免过于频繁
                await asyncio.sleep(10)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Connection timeout task failed: {e}")
        finally:
            self.logger.bind(tag=TAG).info("Connection timeout task exited")

    async def _check_aec_cache_expiry(self):
        """定期清理过期的AEC缓存"""
        try:
            while not self.stop_event.is_set():
                if hasattr(self, "aec_audio_cache") and self.aec_audio_cache:
                    current_time = time.time()
                    expired_keys = [
                        ts for ts, cache_time in list(self.aec_audio_cache_time.items())
                        if current_time - cache_time > 120  # 2分钟过期
                    ]
                    for ts in expired_keys:
                        self.aec_audio_cache.pop(ts, None)
                        self.aec_audio_cache_time.pop(ts, None)
                    if expired_keys:
                        self.logger.bind(tag=TAG).debug(f"[AEC] Removed {len(expired_keys)} expired cache entries")
                # 每30秒检查一次
                await asyncio.sleep(30)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"AEC cache cleanup task failed: {e}")

    def _merge_tool_calls(self, tool_calls_list, tools_call):
        """合并工具调用列表

        Args:
            tool_calls_list: 已收集的工具调用列表
            tools_call: 新的工具调用
        """
        for tool_call in tools_call:
            tool_index = getattr(tool_call, "index", None)
            if tool_index is None:
                if tool_call.function.name:
                    # 有 function_name，说明是新的工具调用
                    tool_index = len(tool_calls_list)
                else:
                    tool_index = len(tool_calls_list) - 1 if tool_calls_list else 0

            # 确保列表有足够的位置
            if tool_index >= len(tool_calls_list):
                tool_calls_list.append({"id": "", "name": "", "arguments": ""})

            # 更新工具调用信息
            if tool_call.id:
                tool_calls_list[tool_index]["id"] = tool_call.id
            if tool_call.function.name:
                tool_calls_list[tool_index]["name"] = tool_call.function.name
            if tool_call.function.arguments:
                tool_calls_list[tool_index]["arguments"] += tool_call.function.arguments
