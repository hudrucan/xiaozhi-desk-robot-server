import asyncio
import threading
import time
from collections import deque
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

from config.logger import setup_logging
from core.handle.receiveAudioHandle import startToChat
from core.handle.sendAudioHandle import (
    send_stt_clear_message,
    send_stt_partial_message,
    stt_final_presentation_sender,
)
from core.providers.asr.base import ASRProviderBase
from core.providers.asr.dto.dto import InterfaceType


TAG = __name__
logger = setup_logging()
SAMPLE_RATE = 16000


class _StaleFinalPresentation(Exception):
    """The owning turn was invalidated before its final STT send."""


class _ConnectionStream:
    """Connection-owned state; the shared provider only owns the model engine."""

    def __init__(self, pre_roll_frames):
        self.lock = asyncio.Lock()
        self.pre_roll = deque(maxlen=pre_roll_frames)
        self.stream = None
        self.active = False
        self.finalizing = False
        self.draining = False
        self.closed = False
        self.last_partial_text = ""
        self.last_partial_sent_at = 0.0
        self.final_presentation_committed = False


class ASRProvider(ASRProviderBase):
    interface_type = InterfaceType.STREAM
    shareable_local = True

    def __init__(self, config: dict, delete_audio_file: bool):
        super().__init__()
        self.sentence_case = config.get("sentence_case", True)
        self.pre_roll_frames = int(config.get("pre_roll_frames", 10))
        self.final_padding_ms = int(config.get("final_padding_ms", 0))
        self.partial_min_interval_ms = int(config.get("partial_min_interval_ms", 120))
        if self.pre_roll_frames < 1:
            raise ValueError("Sherpa streaming pre_roll_frames must be at least 1")
        if self.final_padding_ms < 0:
            raise ValueError("Sherpa streaming final_padding_ms must not be negative")
        if self.partial_min_interval_ms < 0:
            raise ValueError("Sherpa streaming partial_min_interval_ms must not be negative")

        required = ("model_dir", "encoder", "decoder", "joiner", "tokens")
        missing_config = [key for key in required if not config.get(key)]
        if missing_config:
            raise ValueError(
                "Missing Sherpa streaming configuration: " + ", ".join(missing_config)
            )
        model_dir = Path(config["model_dir"])
        if not model_dir.is_dir():
            raise FileNotFoundError(f"Missing Sherpa streaming model directory: {model_dir}")
        paths = {key: model_dir / config[key] for key in required[1:]}
        missing_files = [str(path) for path in paths.values() if not path.is_file()]
        if missing_files:
            raise FileNotFoundError(
                "Missing Sherpa streaming model file(s): " + ", ".join(missing_files)
            )

        try:
            import sherpa_onnx
        except ImportError as error:
            raise RuntimeError(
                "Sherpa streaming ASR requires the optional sherpa-onnx package"
            ) from error

        self._engine_lock = threading.Lock()
        self.recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            **{key: str(path) for key, path in paths.items()},
            sample_rate=SAMPLE_RATE,
            feature_dim=80,
            num_threads=max(1, int(config.get("num_threads", 2))),
            provider=config.get("provider", "cpu"),
            decoding_method=config.get("decoding_method", "greedy_search"),
            enable_endpoint_detection=False,
        )

    async def open_audio_channels(self, conn):
        conn._sherpa_streaming_state = _ConnectionStream(self.pre_roll_frames)
        await super().open_audio_channels(conn)

    async def reset_stream(self, conn):
        state = getattr(conn, "_sherpa_streaming_state", None)
        if state is None:
            return
        # Invalidate immediately, before awaiting any native work. An old final
        # must never dispatch into the new listen/start turn.
        state.closed = True
        conn._sherpa_streaming_state = None
        while True:
            try:
                conn.asr_audio_queue.get_nowait()
                conn.asr_audio_queue.task_done()
            except asyncio.QueueEmpty:
                break
        async with state.lock:
            await self._clear_partial(conn, state)
            await self._reset(conn, state)
            state.finalizing = False
        conn._sherpa_streaming_state = _ConnectionStream(self.pre_roll_frames)

    def _locked_operation(self, operation, *args):
        # Protect every recognizer/OnlineStream call across shared connections.
        with self._engine_lock:
            return operation(*args)

    async def _run_engine(self, operation, *args):
        worker = asyncio.create_task(
            asyncio.to_thread(self._locked_operation, operation, *args)
        )
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            # to_thread cannot stop native decode. Join it before releasing the
            # connection lock, so cleanup cannot race the still-running worker.
            try:
                await worker
            except Exception:
                pass
            raise

    def _drain(self, stream):
        while self.recognizer.is_ready(stream):
            self.recognizer.decode_stream(stream)

    def _feed(self, state, pcm_bytes):
        if state.stream is None:
            state.stream = self.recognizer.create_stream()
        samples = np.frombuffer(pcm_bytes, dtype="<i2").astype(np.float32)
        samples /= 32768.0
        state.stream.accept_waveform(SAMPLE_RATE, samples)
        self._drain(state.stream)
        return self._normalize_text(self.recognizer.get_result(state.stream))

    def _normalize_text(self, text):
        text = text.strip()
        if text and self.sentence_case:
            text = text.lower()
            text = text[0].upper() + text[1:]
        return text

    def _finish(self, state):
        stream = state.stream
        if self.final_padding_ms:
            # input_finished() flushes features, but does not make a partial
            # encoder chunk ready. Supply right context before the final drain.
            stream.accept_waveform(
                SAMPLE_RATE,
                np.zeros(SAMPLE_RATE * self.final_padding_ms // 1000, dtype=np.float32),
            )
        stream.input_finished()
        self._drain(stream)
        # sherpa-onnx 1.13.8's Python get_result returns a string, not a DTO.
        return self._normalize_text(self.recognizer.get_result(stream))

    @staticmethod
    def _reset_partial_state(state):
        state.last_partial_text = ""
        state.last_partial_sent_at = 0.0
        state.final_presentation_committed = False

    async def _send_partial(self, conn, state, text):
        if not text or text == state.last_partial_text or state.closed:
            return
        now = time.monotonic()
        if state.last_partial_text and (
            (now - state.last_partial_sent_at) * 1000 < self.partial_min_interval_ms
        ):
            return
        try:
            if await send_stt_partial_message(conn, text):
                state.last_partial_text = text
                state.last_partial_sent_at = time.monotonic()
        except Exception as error:
            logger.bind(tag=TAG).warning(f"Failed to send live ASR partial: {error}")

    async def _clear_partial(self, conn, state):
        if state.last_partial_text and not state.final_presentation_committed:
            try:
                await send_stt_clear_message(conn)
            except Exception as error:
                logger.bind(tag=TAG).warning(f"Failed to clear live ASR partial: {error}")
                # Keep ownership so subsequent cleanup can retry the clear.
                return
        self._reset_partial_state(state)

    @staticmethod
    def _drop_stream(state):
        state.stream = None

    async def _reset(self, conn, state):
        await self._run_engine(self._drop_stream, state)
        state.active = False
        state.pre_roll.clear()
        if not state.closed:
            conn.reset_audio_states()

    async def _fail(self, conn, state, error):
        logger.bind(tag=TAG).error(f"Sherpa streaming ASR failed: {error}")
        if not state.closed and (state.active or state.finalizing):
            conn.complete_turn_metrics("asr_failed")
        await self._clear_partial(conn, state)
        await self._reset(conn, state)

    @staticmethod
    def _release_finished_turn(conn, state, audio_task):
        if conn.persistent_websocket and not conn.client_listening and not state.closed:
            asyncio.create_task(conn.release_turn_asr(expected_audio_task=audio_task))

    async def receive_audio(self, conn, pcm_frame, audio_have_voice):
        state = getattr(conn, "_sherpa_streaming_state", None)
        if state is None or state.closed or state.finalizing:
            return

        should_finalize = False
        async with state.lock:
            if state.closed or state.finalizing:
                return
            try:
                if not state.active:
                    state.pre_roll.append(pcm_frame)
                    if not audio_have_voice:
                        return
                    if not conn.has_active_turn_metrics():
                        conn.start_turn_metrics("voice")
                    conn.mark_turn_metric("speech_start")
                    conn.mark_turn_metric("asr_start")
                    state.active = True
                    # The first speech frame is already included in pre-roll.
                    pcm_bytes = b"".join(state.pre_roll)
                    state.pre_roll.clear()
                else:
                    pcm_bytes = pcm_frame
                partial = await self._run_engine(self._feed, state, pcm_bytes)
                if state.closed:
                    return
                # Connection serialization extends through the send, but the
                # native engine lock has already been released. No queued
                # partial task can overtake final/reset presentation.
                await self._send_partial(conn, state, partial)
                should_finalize = conn.client_voice_stop and not state.draining
            except Exception as error:
                await self._fail(conn, state, error)

        if should_finalize:
            await self.finalize_stream(conn)

    async def finalize_stream(self, conn) -> bool:
        state = getattr(conn, "_sherpa_streaming_state", None)
        if state is None or state.closed:
            return False
        if state.finalizing or state.draining:
            return True

        audio_task = conn.asr_audio_task
        if asyncio.current_task() is not audio_task:
            # listen/stop may arrive before the PCM consumer catches up. Keep
            # those queued frames in order before finishing the OnlineStream.
            state.draining = True
            try:
                await conn.asr_audio_queue.join()
            finally:
                state.draining = False

        async with state.lock:
            if state.closed or getattr(conn, "_sherpa_streaming_state", None) is not state:
                return False
            if state.finalizing:
                return True
            if not state.active:
                return False
            state.finalizing = True
            try:
                conn.mark_turn_metric("speech_end")
                transcript = await self._run_engine(self._finish, state)
                if state.closed:
                    return True
                conn.mark_turn_metric("asr_done")
                if not transcript:
                    await self._clear_partial(conn, state)
                await self._reset(conn, state)
            except Exception as error:
                await self._fail(conn, state, error)
                state.finalizing = False
                self._release_finished_turn(conn, state, audio_task)
                return True

        # Keep finalizing set through downstream handoff, preventing a second
        # final or a new stream while the accepted input is being dispatched.
        presentation_task = asyncio.current_task()

        async def send_final_presentation(final_conn, message):
            if final_conn is not conn or asyncio.current_task() is not presentation_task:
                await final_conn.websocket.send(message)
                return
            async with state.lock:
                if (
                    state.closed
                    or getattr(conn, "_sherpa_streaming_state", None) is not state
                    or conn.stop_event.is_set()
                ):
                    raise _StaleFinalPresentation()
                # Serialize final against clear/reset, without the engine lock.
                await conn.websocket.send(message)
                state.final_presentation_committed = True

        presentation_token = stt_final_presentation_sender.set(send_final_presentation)
        try:
            if (
                state.closed
                or getattr(conn, "_sherpa_streaming_state", None) is not state
                or conn.stop_event.is_set()
            ):
                return True
            if not transcript:
                conn.complete_turn_metrics("empty_asr_result")
                return True
            logger.bind(tag=TAG).info(f"Recognized text: {transcript}")
            await startToChat(conn, transcript)
        except _StaleFinalPresentation:
            return True
        finally:
            stt_final_presentation_sender.reset(presentation_token)
            async with state.lock:
                # Successful final releases ownership without clear. Empty,
                # failed, stale or intent-only handoffs still owe a clear.
                await self._clear_partial(conn, state)
                state.finalizing = False
            self._release_finished_turn(conn, state, audio_task)
        return True

    async def close_audio_channels(self, conn):
        state = getattr(conn, "_sherpa_streaming_state", None)
        if state is None:
            return
        state.closed = True
        conn._sherpa_streaming_state = None
        async with state.lock:
            await self._clear_partial(conn, state)
            await self._reset(conn, state)
            state.finalizing = False
            state.draining = False
            conn.reset_audio_states()

    async def speech_to_text(
        self,
        opus_data: List[bytes],
        session_id: str,
        artifacts: Optional[ASRProviderBase.AudioArtifacts] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        raise RuntimeError("Sherpa streaming ASR only supports streaming audio")
