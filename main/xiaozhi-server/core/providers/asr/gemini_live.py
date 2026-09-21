import asyncio
import contextlib
import time
from collections import deque
from typing import List, Optional, Tuple

from google import genai
from google.genai import types

from config.logger import setup_logging
from core.handle.receiveAudioHandle import startToChat
from core.providers.asr.base import ASRProviderBase
from core.providers.asr.dto.dto import InterfaceType


TAG = __name__
logger = setup_logging()


class ASRProvider(ASRProviderBase):
    def __init__(self, config: dict, delete_audio_file: bool):
        super().__init__()
        self.interface_type = InterfaceType.STREAM
        self.api_key = config.get("api_key")
        if not self.api_key or self.api_key in {"your_api_key", "你的api_key"}:
            raise ValueError("Gemini Live ASR requires a valid API key")

        self.model_name = config.get(
            "model_name", "gemini-3.5-transcribe-live"
        )
        self.language = config.get("language", "auto")
        self.mode = str(config.get("mode", "VERBATIM")).upper()
        self.audio_queue_size = int(config.get("audio_queue_size", 100))
        self.session_refresh_seconds = int(
            config.get("session_refresh_seconds", 540)
        )
        self.chunk_bytes = int(config.get("chunk_ms", 120)) * 32
        self.delete_audio_file = delete_audio_file

        self._client = genai.Client(api_key=self.api_key)
        self._conn = None
        self._session_context = None
        self._session = None
        self._session_started_at = 0.0
        self._connect_lock = asyncio.Lock()
        self._send_queue = asyncio.Queue(maxsize=self.audio_queue_size)
        self._sender_task = None
        self._receiver_task = None
        self._reconnect_task = None
        self._closed = False

        self._pre_roll = deque(maxlen=10)
        self._pcm_buffer = bytearray()
        self._stream_active = False
        self._ending_turn = False
        self._awaiting_final = False
        self._turn_number = 0

    async def open_audio_channels(self, conn):
        self._conn = conn
        await super().open_audio_channels(conn)
        self._sender_task = asyncio.create_task(self._sender_loop())
        try:
            await self._ensure_session(refresh_if_old=False)
        except Exception as error:
            logger.bind(tag=TAG).warning(
                f"Gemini Live ASR will connect on first speech: {error}"
            )

    def _live_config(self):
        language_codes = [] if self.language == "auto" else [self.language]
        return types.LiveConnectConfig(
            response_modalities=["TEXT"],
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=True
                )
            ),
            input_audio_transcription=types.AudioTranscriptionConfig(
                language_codes=language_codes,
                mode=self.mode,
            ),
        )

    async def _ensure_session(self, refresh_if_old=True, log_connected=True):
        async with self._connect_lock:
            session_age = time.monotonic() - self._session_started_at
            refresh_required = (
                refresh_if_old
                and self._session is not None
                and session_age >= self.session_refresh_seconds
            )
            if self._session is not None and not refresh_required:
                return self._session

            await self._close_session()
            self._session_context = self._client.aio.live.connect(
                model=self.model_name,
                config=self._live_config(),
            )
            self._session = await self._session_context.__aenter__()
            self._session_started_at = time.monotonic()
            self._receiver_task = asyncio.create_task(self._receiver_loop())
            if log_connected:
                logger.bind(tag=TAG).info("Gemini Live ASR session connected")
            return self._session

    async def receive_audio(self, conn, pcm_frame, audio_have_voice):
        if self._closed or self._ending_turn or self._awaiting_final:
            return

        if not self._stream_active:
            self._pre_roll.append(pcm_frame)
            if not audio_have_voice:
                return

            self._stream_active = True
            self._turn_number += 1
            await self._send_queue.put(("start", self._turn_number))
            for buffered_frame in self._pre_roll:
                await self._buffer_pcm(buffered_frame)
            self._pre_roll.clear()
        else:
            await self._buffer_pcm(pcm_frame)

        if conn.client_voice_stop:
            await self._send_stop_request()

    async def _buffer_pcm(self, pcm_frame):
        self._pcm_buffer.extend(pcm_frame)
        while len(self._pcm_buffer) >= self.chunk_bytes:
            chunk = bytes(self._pcm_buffer[: self.chunk_bytes])
            del self._pcm_buffer[: self.chunk_bytes]
            await self._send_queue.put(("audio", chunk))

    async def _send_stop_request(self):
        if not self._stream_active or self._ending_turn or self._awaiting_final:
            return

        if self._pcm_buffer:
            await self._send_queue.put(("audio", bytes(self._pcm_buffer)))
            self._pcm_buffer.clear()
        await self._send_queue.put(("end", self._turn_number))
        self._stream_active = False
        self._ending_turn = True

    async def _sender_loop(self):
        while not self._closed:
            try:
                event_type, value = await self._send_queue.get()
                session = await self._ensure_session(
                    refresh_if_old=event_type == "start"
                )
                if event_type == "start":
                    await session.send_realtime_input(
                        activity_start=types.ActivityStart()
                    )
                elif event_type == "audio":
                    await session.send_realtime_input(
                        audio=types.Blob(
                            data=value,
                            mime_type="audio/pcm;rate=16000",
                        )
                    )
                elif event_type == "end":
                    # All queued audio has been sent when this event is reached.
                    self._ending_turn = False
                    self._awaiting_final = True
                    await session.send_realtime_input(
                        activity_end=types.ActivityEnd()
                    )
            except asyncio.CancelledError:
                break
            except Exception as error:
                logger.bind(tag=TAG).error(
                    f"Gemini Live ASR audio send failed: {error}"
                )
                await self._discard_active_turn()
                await self._close_session()

    async def _receiver_loop(self):
        try:
            while not self._closed and self._session is not None:
                async for response in self._session.receive():
                    server_content = response.server_content
                    if not server_content:
                        continue

                    final = server_content.input_transcription
                    if final and final.text and self._awaiting_final:
                        await self._handle_final_transcript(final.text)
                    elif server_content.turn_complete and self._awaiting_final:
                        await self._handle_final_transcript("")
        except asyncio.CancelledError:
            pass
        except Exception as error:
            if not self._closed:
                turn_active = (
                    self._stream_active
                    or self._ending_turn
                    or self._awaiting_final
                )
                is_policy_close = (
                    getattr(error, "code", None) == 1008
                    or str(error).startswith("1008 ")
                )
                idle_close = is_policy_close and not turn_active
                if idle_close:
                    logger.bind(tag=TAG).debug(
                        "Gemini Live ASR idle session closed"
                    )
                else:
                    logger.bind(tag=TAG).error(
                        f"Gemini Live ASR receive failed: {error}"
                    )
                self._receiver_task = None
                self._session = None
                await self._discard_active_turn()
                if idle_close:
                    self._reconnect_task = asyncio.create_task(
                        self._reconnect_after_idle_close()
                    )

    async def _reconnect_after_idle_close(self):
        try:
            await asyncio.sleep(0)
            if self._closed:
                return
            await self._ensure_session(
                refresh_if_old=False,
                log_connected=False,
            )
            logger.bind(tag=TAG).info(
                "Gemini Live ASR session reconnected after idle timeout"
            )
        except asyncio.CancelledError:
            pass
        except Exception as error:
            if not self._closed:
                logger.bind(tag=TAG).warning(
                    f"Gemini Live ASR idle reconnect failed: {error}"
                )
        finally:
            self._reconnect_task = None

    async def _handle_final_transcript(self, text):
        if not self._awaiting_final or self._conn is None:
            return

        transcript = text.strip()
        self._awaiting_final = False
        self._ending_turn = False
        self._pre_roll.clear()
        self._conn.reset_audio_states()
        if not transcript or self._conn.stop_event.is_set():
            return

        logger.bind(tag=TAG).info(f"Recognized text: {transcript}")
        await startToChat(self._conn, transcript)

    async def _discard_active_turn(self):
        self._stream_active = False
        self._ending_turn = False
        self._awaiting_final = False
        self._pcm_buffer.clear()
        self._pre_roll.clear()
        while not self._send_queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._send_queue.get_nowait()
        if self._conn is not None:
            self._conn.reset_audio_states()

    async def _close_session(self):
        reconnect_task = self._reconnect_task
        self._reconnect_task = None
        if (
            reconnect_task is not None
            and reconnect_task is not asyncio.current_task()
            and not reconnect_task.done()
        ):
            reconnect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reconnect_task

        receiver_task = self._receiver_task
        self._receiver_task = None
        if (
            receiver_task is not None
            and receiver_task is not asyncio.current_task()
            and not receiver_task.done()
        ):
            receiver_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await receiver_task

        session_context = self._session_context
        self._session = None
        self._session_context = None
        self._session_started_at = 0.0
        if session_context is not None:
            with contextlib.suppress(Exception):
                await session_context.__aexit__(None, None, None)

    async def speech_to_text(
        self,
        opus_data: List[bytes],
        session_id: str,
        artifacts: Optional[ASRProviderBase.AudioArtifacts] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        raise RuntimeError("Gemini Live ASR only supports streaming audio")

    async def close(self):
        if self._closed:
            return
        self._closed = True

        if self._sender_task is not None and not self._sender_task.done():
            self._sender_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sender_task
        self._sender_task = None
        await self._close_session()
        await self._discard_active_turn()
        await self._client.aio.aclose()
