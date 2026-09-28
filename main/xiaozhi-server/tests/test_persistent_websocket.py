import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import time
import unittest
from unittest.mock import AsyncMock

from core.connected_devices import (
    ConnectedDeviceRegistry,
    register_connected_device_after_hello,
)
from core.handle.textHandler.goodbyeMessageHandler import GoodbyeMessageHandler
from core.persistent_websocket import negotiate_persistent_websocket
from core.utils.runtime_diagnostics import RuntimeDiagnostics


class _Handler:
    def __init__(self):
        self.sent = []
        self.closed = False

    async def send_json(self, payload):
        self.sent.append(payload)
        return True

    async def close(self):
        self.closed = True


class PersistentWebSocketTests(unittest.IsolatedAsyncioTestCase):
    def test_runtime_diagnostics_tracks_transport_heartbeat_and_live_mcp(self):
        diagnostics = RuntimeDiagnostics()
        diagnostics.register_connection("session", device_id="robot")

        initial = diagnostics.snapshot(include_history=False)["connections"]["items"][0]
        self.assertFalse(initial["persistent_websocket"])
        self.assertIsNone(initial["last_heartbeat_at"])
        self.assertFalse(initial["mcp_ready"])
        self.assertEqual(initial["mcp_tool_count"], 0)

        diagnostics.update_connection_transport("session", True)
        diagnostics.record_heartbeat("session")
        diagnostics.update_mcp("session", True, 24)

        connection = diagnostics.snapshot(include_history=False)["connections"]["items"][0]
        self.assertTrue(connection["persistent_websocket"])
        self.assertIsNotNone(connection["last_heartbeat_at"])
        self.assertTrue(connection["mcp_ready"])
        self.assertEqual(connection["mcp_tool_count"], 24)

    async def test_registry_newest_wins_and_stale_cleanup_is_ignored(self):
        registry = ConnectedDeviceRegistry()
        old = _Handler()
        new = _Handler()

        self.assertIsNone(await registry.register("robot", old))
        self.assertIs(await registry.register("robot", new), old)
        self.assertFalse(await registry.unregister("robot", old))
        self.assertIs(await registry.get("robot"), new)
        self.assertTrue(await registry.send("robot", {"type": "notify"}))
        self.assertEqual(new.sent, [{"type": "notify"}])
        self.assertTrue(await registry.unregister("robot", new))

    async def test_incomplete_hello_does_not_evict_registered_handler(self):
        registry = ConnectedDeviceRegistry()
        healthy = _Handler()
        candidate = _Handler()
        await registry.register("robot", healthy)

        # Merely constructing/authenticating a candidate changes nothing.
        self.assertIs(await registry.get("robot"), healthy)
        self.assertFalse(healthy.closed)

        previous = await register_connected_device_after_hello(
            "robot", candidate, registry
        )
        await asyncio.sleep(0)
        self.assertIs(previous, healthy)
        self.assertIs(await registry.get("robot"), candidate)
        self.assertTrue(healthy.closed)

    async def test_capability_requires_client_support_and_websocket_transport(self):
        self.assertTrue(
            negotiate_persistent_websocket(
                {"desk_robot_persistent_ws_v1": True}, False
            )
        )
        self.assertFalse(negotiate_persistent_websocket({}, False))
        self.assertFalse(
            negotiate_persistent_websocket(
                {"desk_robot_persistent_ws_v1": True}, True
            )
        )

    async def test_client_goodbye_ends_only_the_logical_conversation(self):
        calls = []
        conn = type("Conn", (), {})()

        async def end_conversation(reason, notify_client=True):
            calls.append((reason, notify_client))

        conn.end_conversation = end_conversation
        await GoodbyeMessageHandler().handle(conn, {"type": "goodbye"})
        self.assertEqual(calls, [("client_goodbye", False)])

    def test_direct_exit_uses_logical_conversation_end(self):
        source = (
            Path(__file__).parents[1] / "core" / "handle" / "intentHandler.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "check_direct_exit"
        )
        awaited_methods = {
            node.value.func.attr
            for node in ast.walk(function)
            if isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
        }
        self.assertIn("end_conversation", awaited_methods)
        self.assertNotIn("close", awaited_methods)

    def test_persistent_idle_binary_ingress_is_dropped_before_audio_setup(self):
        source = (Path(__file__).parents[1] / "core" / "connection.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        route = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_route_message"
        )
        binary_branch = next(
            node
            for node in ast.walk(route)
            if isinstance(node, ast.If)
            and ast.unparse(node.test) == "isinstance(message, bytes)"
        )
        idle_guard = binary_branch.body[0]
        self.assertIsInstance(idle_guard, ast.If)
        self.assertEqual(
            ast.unparse(idle_guard.test),
            "self.persistent_websocket and (not self.client_listening)",
        )
        self.assertEqual(len(idle_guard.body), 1)
        self.assertIsInstance(idle_guard.body[0], ast.Return)

        # Listening persistent sessions and all legacy/MQTT sessions fall
        # through to the same idempotent audio-channel initialization.
        audio_setup = binary_branch.body[1]
        self.assertIsInstance(audio_setup, ast.Expr)
        self.assertIsInstance(audio_setup.value, ast.Await)
        self.assertEqual(
            audio_setup.value.value.func.attr,
            "ensure_turn_audio_channels",
        )

    async def test_accepted_persistent_input_stops_listening_before_chat(self):
        source = (
            Path(__file__).parents[1] / "core" / "handle" / "receiveAudioHandle.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        start_to_chat_node = next(
            node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "startToChat"
        )
        namespace = {
            "json": json,
            "handle_user_intent": AsyncMock(return_value=True),
            "handleAbortMessage": AsyncMock(),
            "send_stt_message": AsyncMock(),
        }
        exec(
            compile(
                ast.fix_missing_locations(
                    ast.Module(body=[start_to_chat_node], type_ignores=[])
                ),
                "<startToChat>",
                "exec",
            ),
            namespace,
        )
        conn = SimpleNamespace(
            persistent_websocket=True,
            client_listening=True,
            has_active_turn_metrics=lambda: True,
            introduced_speakers=set(),
            current_speaker=None,
            client_is_speaking=False,
            client_listen_mode="auto",
        )
        await namespace["startToChat"](conn, "accepted transcript")

        self.assertFalse(conn.client_listening)

    async def test_tts_stop_end_conversation_flag_is_explicit_and_optional(self):
        source = (
            Path(__file__).parents[1] / "core" / "handle" / "sendAudioHandle.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        send_tts_node = next(
            node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "send_tts_message"
        )
        namespace = {
            "json": json,
            "text_utils": SimpleNamespace(remove_emojis=lambda text: text),
            "audio_to_data": AsyncMock(),
            "sendAudio": AsyncMock(),
            "_wait_for_audio_completion": AsyncMock(),
            "send_status_message": AsyncMock(),
            "time": time,
        }
        exec(
            compile(
                ast.fix_missing_locations(
                    ast.Module(body=[send_tts_node], type_ignores=[])
                ),
                "<send_tts_message>",
                "exec",
            ),
            namespace,
        )

        class WebSocket:
            def __init__(self):
                self.messages = []

            async def send(self, message):
                self.messages.append(json.loads(message))

        websocket = WebSocket()
        conn = SimpleNamespace(
            session_id="session",
            sentence_id="sentence",
            config={},
            websocket=websocket,
            clearSpeakStatus=lambda: None,
            mark_turn_metric=lambda _name: None,
            complete_turn_metrics=lambda _status: None,
        )

        await namespace["send_tts_message"](conn, "stop")
        await namespace["send_tts_message"](
            conn, "stop", end_conversation=True
        )

        self.assertNotIn("end_conversation", websocket.messages[0])
        self.assertIs(websocket.messages[1]["end_conversation"], True)

    def test_farewell_tts_stop_flag_comes_only_from_close_after_chat(self):
        source = (
            Path(__file__).parents[1] / "core" / "handle" / "sendAudioHandle.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        send_audio_message = next(
            node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "sendAudioMessage"
        )
        assignment = next(
            node
            for node in ast.walk(send_audio_message)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "end_conversation"
                for target in node.targets
            )
        )
        self.assertEqual(
            ast.unparse(assignment.value),
            "bool(conn.close_after_chat)",
        )

    def test_persistent_no_speech_stop_releases_turn_asr(self):
        root = Path(__file__).parents[1]
        listen_tree = ast.parse(
            (root / "core" / "handle" / "textHandler" / "listenMessageHandler.py")
            .read_text(encoding="utf-8")
        )
        handle = next(
            node
            for node in ast.walk(listen_tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle"
        )
        no_speech_release = next(
            node
            for node in ast.walk(handle)
            if isinstance(node, ast.If)
            and ast.unparse(node.test)
            == "conn.persistent_websocket and finalization_pending is False"
        )
        self.assertEqual(len(no_speech_release.body), 1)
        release_call = no_speech_release.body[0]
        self.assertIsInstance(release_call, ast.Expr)
        self.assertIsInstance(release_call.value, ast.Await)
        self.assertEqual(
            release_call.value.value.func.attr,
            "release_turn_asr",
        )

        gemini_tree = ast.parse(
            (root / "core" / "providers" / "asr" / "gemini.py")
            .read_text(encoding="utf-8")
        )
        stop_request = next(
            node
            for node in ast.walk(gemini_tree)
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_send_stop_request"
        )
        no_active_stream = next(
            node
            for node in ast.walk(stop_request)
            if isinstance(node, ast.If)
            and ast.unparse(node.test) == "not self._stream_active"
        )
        self.assertIsInstance(no_active_stream.body[0], ast.Return)
        self.assertIs(no_active_stream.body[0].value.value, False)

    async def test_turn_asr_release_drains_only_stale_asr_audio(self):
        source = (Path(__file__).parents[1] / "core" / "connection.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        close_turn_audio = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_close_turn_audio_channels"
        )
        called_methods = {
            node.func.attr
            for node in ast.walk(close_turn_audio)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertIn("get_nowait", called_methods)
        self.assertIn("task_done", called_methods)
        self.assertNotIn("clear_queues", called_methods)

        namespace = {"asyncio": asyncio}
        exec(
            compile(
                ast.fix_missing_locations(
                    ast.Module(body=[close_turn_audio], type_ignores=[])
                ),
                "<close_turn_audio_channels>",
                "exec",
            ),
            namespace,
        )
        close_turn_audio_channels = namespace["_close_turn_audio_channels"]

        persistent_queue = asyncio.Queue()
        persistent_queue.put_nowait(b"old-1")
        persistent_queue.put_nowait(b"old-2")
        persistent = SimpleNamespace(
            asr_audio_task=None,
            asr_audio_queue=persistent_queue,
            persistent_websocket=True,
            asr=None,
            asr_channel_open=True,
        )
        await close_turn_audio_channels(persistent)
        self.assertTrue(persistent_queue.empty())
        await asyncio.wait_for(persistent_queue.join(), timeout=0.1)

        legacy_queue = asyncio.Queue()
        legacy_queue.put_nowait(b"legacy")
        legacy = SimpleNamespace(
            asr_audio_task=None,
            asr_audio_queue=legacy_queue,
            persistent_websocket=False,
            asr=None,
            asr_channel_open=True,
        )
        await close_turn_audio_channels(legacy)
        self.assertEqual(legacy_queue.qsize(), 1)


if __name__ == "__main__":
    unittest.main()
