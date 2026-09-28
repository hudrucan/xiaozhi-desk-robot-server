import ast
import asyncio
import contextlib
from collections import deque
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock


class _BoundLogger:
    def debug(self, _message):
        pass

    def info(self, _message):
        pass

    def warning(self, _message):
        pass

    def error(self, _message):
        pass


class _Logger:
    def bind(self, **_kwargs):
        return _BoundLogger()


async def _start_to_chat(_conn, _text):
    return None


def _load_watchdog_harness():
    source = (
        Path(__file__).parents[1] / "core" / "providers" / "asr" / "gemini.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    provider = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ASRProvider"
    )
    method_names = {
        "_arm_finalization_watchdog",
        "_cancel_finalization_watchdog",
        "_finalization_watchdog",
        "_send_stop_request",
        "_receiver_loop",
        "_handle_final_transcript",
        "_discard_active_turn",
        "_close_session",
        "close",
    }
    namespace = {
        "asyncio": asyncio,
        "contextlib": contextlib,
        "FINALIZATION_TIMEOUT_SECONDS": 0.01,
        "logger": _Logger(),
        "TAG": "gemini-test",
        "startToChat": _start_to_chat,
    }

    class Harness:
        pass

    for node in provider.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name in method_names:
            exec(
                compile(
                    ast.fix_missing_locations(
                        ast.Module(body=[node], type_ignores=[])
                    ),
                    f"<{node.name}>",
                    "exec",
                ),
                namespace,
            )
            setattr(Harness, node.name, namespace[node.name])
    return Harness


Harness = _load_watchdog_harness()


class _SessionContext:
    def __init__(self):
        self.exit = AsyncMock()

    async def __aexit__(self, exc_type, exc, traceback):
        await self.exit(exc_type, exc, traceback)


class GeminiFinalizationWatchdogTests(unittest.IsolatedAsyncioTestCase):
    def _provider(self):
        provider = Harness()
        provider._closed = False
        provider._turn_number = 1
        provider._stream_active = False
        provider._ending_turn = False
        provider._awaiting_final = False
        provider._finalization_watchdog_task = None
        provider._reconnect_task = None
        provider._receiver_task = None
        provider._sender_task = None
        provider._session = None
        provider._session_context = None
        provider._session_started_at = 0.0
        provider._pre_roll = deque()
        provider._pcm_buffer = bytearray()
        provider._send_queue = asyncio.Queue()
        provider._client = SimpleNamespace(
            aio=SimpleNamespace(aclose=AsyncMock())
        )
        provider._conn = SimpleNamespace(
            persistent_websocket=True,
            client_listening=True,
            stop_event=asyncio.Event(),
            mark_turn_metric=lambda _name: None,
            reset_audio_states=lambda: None,
            complete_turn_metrics=lambda _status: None,
            release_turn_asr=AsyncMock(),
            end_conversation=AsyncMock(),
            reset_conversation=AsyncMock(),
        )
        return provider

    async def test_normal_final_transcript_cancels_watchdog(self):
        provider = self._provider()
        provider._awaiting_final = True
        await provider._arm_finalization_watchdog(provider._turn_number)
        watchdog = provider._finalization_watchdog_task
        response = SimpleNamespace(
            server_content=SimpleNamespace(
                input_transcription=SimpleNamespace(text="hello"),
                turn_complete=False,
            )
        )

        class Session:
            async def receive(self_inner):
                yield response
                provider._closed = True

        provider._session = Session()
        provider._receiver_task = asyncio.current_task()
        await provider._receiver_loop()
        await asyncio.sleep(0)

        self.assertIsNone(provider._finalization_watchdog_task)
        self.assertTrue(watchdog.done())
        provider._closed = False

    async def test_turn_complete_waits_for_independent_input_transcription(self):
        provider = self._provider()
        provider._awaiting_final = True
        await provider._arm_finalization_watchdog(provider._turn_number)
        watchdog = provider._finalization_watchdog_task
        response = SimpleNamespace(
            server_content=SimpleNamespace(
                input_transcription=None,
                turn_complete=True,
            )
        )

        class Session:
            async def receive(self_inner):
                yield response
                provider._closed = True

        provider._session = Session()
        provider._receiver_task = asyncio.current_task()
        await provider._receiver_loop()
        await asyncio.sleep(0)

        self.assertIs(provider._finalization_watchdog_task, watchdog)
        self.assertFalse(watchdog.done())
        self.assertTrue(provider._awaiting_final)
        await provider._cancel_finalization_watchdog()
        provider._closed = False

    async def test_empty_transcript_resets_turn_without_releasing_asr(self):
        provider = self._provider()
        provider._awaiting_final = True
        await provider._arm_finalization_watchdog(provider._turn_number)

        await provider._handle_final_transcript("")
        await asyncio.sleep(0)

        self.assertFalse(provider._awaiting_final)
        self.assertTrue(provider._conn.client_listening)
        provider._conn.release_turn_asr.assert_not_awaited()

    async def test_missing_final_resets_only_stuck_turn_and_session(self):
        provider = self._provider()
        provider._turn_number = 7
        provider._awaiting_final = True
        provider._session = object()
        session_context = _SessionContext()
        provider._session_context = session_context
        provider._sender_task = asyncio.create_task(asyncio.sleep(1))

        await provider._arm_finalization_watchdog(provider._turn_number)
        watchdog = provider._finalization_watchdog_task
        await asyncio.wait_for(watchdog, timeout=0.2)

        self.assertFalse(provider._closed)
        self.assertFalse(provider._stream_active)
        self.assertFalse(provider._ending_turn)
        self.assertFalse(provider._awaiting_final)
        self.assertIsNone(provider._session)
        self.assertIsNone(provider._session_context)
        self.assertFalse(provider._sender_task.done())
        session_context.exit.assert_awaited_once()
        provider._conn.release_turn_asr.assert_not_awaited()
        provider._conn.end_conversation.assert_not_awaited()
        provider._conn.reset_conversation.assert_not_awaited()

        provider._sender_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await provider._sender_task

    async def test_watchdog_recovery_preserves_client_listening(self):
        provider = self._provider()
        provider._ending_turn = True

        await provider._arm_finalization_watchdog(provider._turn_number)
        await asyncio.wait_for(provider._finalization_watchdog_task, timeout=0.2)

        self.assertTrue(provider._conn.client_listening)

    async def test_stale_watchdog_cannot_reset_a_later_turn(self):
        provider = self._provider()
        provider._turn_number = 2
        provider._ending_turn = True
        session_context = _SessionContext()
        provider._session = object()
        provider._session_context = session_context

        stale = asyncio.create_task(provider._finalization_watchdog(1))
        provider._finalization_watchdog_task = stale
        await asyncio.wait_for(stale, timeout=0.2)

        self.assertTrue(provider._ending_turn)
        self.assertIsNotNone(provider._session)
        session_context.exit.assert_not_awaited()

    async def test_provider_close_cancels_watchdog_cleanly(self):
        provider = self._provider()
        provider._ending_turn = True
        await provider._arm_finalization_watchdog(provider._turn_number)
        watchdog = provider._finalization_watchdog_task

        await provider.close()

        self.assertTrue(provider._closed)
        self.assertIsNone(provider._finalization_watchdog_task)
        self.assertTrue(watchdog.done())
        provider._client.aio.aclose.assert_awaited_once()

    async def test_stop_queues_end_without_arming_watchdog(self):
        provider = self._provider()
        provider._conn.persistent_websocket = False

        self.assertFalse(await provider._send_stop_request())
        self.assertIsNone(provider._finalization_watchdog_task)

        provider._stream_active = True
        self.assertTrue(await provider._send_stop_request())
        self.assertTrue(provider._ending_turn)
        self.assertIsNone(provider._finalization_watchdog_task)

        provider._ending_turn = False
        provider._stream_active = True
        provider._conn.persistent_websocket = True
        provider._turn_number += 1
        self.assertTrue(await provider._send_stop_request())
        self.assertIsNone(provider._finalization_watchdog_task)

    def test_sender_arms_watchdog_immediately_before_activity_end(self):
        source = (
            Path(__file__).parents[1]
            / "core"
            / "providers"
            / "asr"
            / "gemini.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        provider = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "ASRProvider"
        )
        sender = next(
            node
            for node in provider.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_sender_loop"
        )
        end_branch = next(
            node
            for node in ast.walk(sender)
            if isinstance(node, ast.If)
            and ast.unparse(node.test) == "event_type == 'end'"
        )
        statements = [ast.unparse(node) for node in end_branch.body]
        arm_index = next(
            index
            for index, statement in enumerate(statements)
            if "_arm_finalization_watchdog(value)" in statement
        )
        send_index = next(
            index
            for index, statement in enumerate(statements)
            if "activity_end=types.ActivityEnd()" in statement
        )
        self.assertEqual(arm_index + 1, send_index)


if __name__ == "__main__":
    unittest.main()
