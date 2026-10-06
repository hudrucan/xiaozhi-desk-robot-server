"""Offline checks for bounded core requests and lifecycle ownership."""

import asyncio
import json
import unittest
from types import SimpleNamespace

from core.cluster.nats_config import NatsConnectionConfig
from core.cluster.protocol import ping_response
from core.cluster.worker_rpc import MAX_INFLIGHT, WorkerRPC, WorkerRpcError, parse_ping_reply


class Client:
    def __init__(self):
        self.is_connected = True
        self.is_closed = False
        self.calls = []
        self.options = None
        self.block = None
        self.reply = ping_response("deskb2x")

    async def connect(self, **options):
        self.options = options

    async def request(self, subject, data, **options):
        self.calls.append((subject, data, options))
        if self.block:
            await self.block.wait()
        return SimpleNamespace(data=self.reply)

    async def drain(self):
        self.is_closed = True

    async def close(self):
        self.is_closed = True


class WorkerRPCTests(unittest.IsolatedAsyncioTestCase):
    def rpc(self):
        self.client = Client()
        config = NatsConnectionConfig(("nats://10.10.10.11:4222", "nats://10.10.10.12:4222", "nats://10.10.10.13:4222"), "application", "private-value")
        return WorkerRPC(config, "deskb1x", client_factory=lambda: self.client)

    async def test_queue_and_targeted_subjects_preserve_worker_contract(self):
        rpc = self.rpc()
        self.assertEqual((await rpc.ping())["worker_id"], "deskb2x")
        await rpc.ping("deskb2x")
        self.assertEqual([call[0] for call in self.client.calls], ["xiaozhi.v1.worker.ping", "xiaozhi.v1.worker.deskb2x.ping"])
        self.assertTrue(all(call[1] == b"" for call in self.client.calls))
        self.assertEqual(rpc.status()["completed"], 2)
        self.assertEqual(rpc.status()["inflight"], 0)

    async def test_no_connection_means_no_buffered_or_retried_request(self):
        rpc = self.rpc()
        self.client.is_connected = False
        with self.assertRaisesRegex(WorkerRpcError, "worker_rpc_unavailable"):
            await rpc.ping()
        self.assertEqual(self.client.calls, [])

    async def test_invalid_identity_never_constructs_subject(self):
        rpc = self.rpc()
        for identity in ("*", "a.b", "a b", "", "a" * 193):
            with self.assertRaises(ValueError):
                await rpc.ping(identity)
        self.assertEqual(self.client.calls, [])

    async def test_reply_boundaries_identity_and_secret_safe_error(self):
        rpc = self.rpc()
        for data in (b"x" * 1025, b"\xff", b"[]", b'{"status":"ok","status":"ok"}',
                     ping_response("wrong-worker")):
            self.client.reply = data
            with self.assertRaisesRegex(WorkerRpcError, "worker_rpc_invalid_reply"):
                await rpc.ping("deskb2x")
        reply = json.loads(ping_response("deskb2x"))
        reply["capabilities"] = ["llm"]
        with self.assertRaises(ValueError):
            parse_ping_reply(json.dumps(reply).encode())
        self.assertNotIn("private-value", json.dumps(rpc.status()))

    async def test_concurrency_is_bounded_and_shutdown_cancels_owned_calls(self):
        rpc = self.rpc()
        self.client.block = asyncio.Event()
        tasks = [asyncio.create_task(rpc.ping()) for _ in range(MAX_INFLIGHT)]
        await asyncio.sleep(0)
        self.assertEqual(rpc.status()["inflight"], MAX_INFLIGHT)
        with self.assertRaisesRegex(WorkerRpcError, "worker_rpc_busy"):
            await rpc.ping()
        await rpc.close()
        self.assertTrue(all(task.cancelled() for task in tasks))
        self.assertEqual(rpc.status()["inflight"], 0)
        self.assertTrue(self.client.is_closed)
        await rpc.close()

    async def test_request_cancellation_clears_admission(self):
        rpc = self.rpc()
        self.client.block = asyncio.Event()
        task = asyncio.create_task(rpc.ping())
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(rpc.status()["inflight"], 0)

    async def test_transport_failure_is_not_retried_and_never_leaks_exception(self):
        rpc = self.rpc()
        async def failed(*args, **kwargs):
            raise RuntimeError("private-value nats://user:secret@host")
        self.client.request = failed
        with self.assertRaisesRegex(WorkerRpcError, "^worker_rpc_failed$"):
            await rpc.ping()
        self.assertEqual(rpc.status()["failed"], 1)

    async def test_indefinite_connection_reconnect_callbacks_and_servers(self):
        rpc = self.rpc()
        rpc.start()
        await rpc.startup
        self.assertEqual(len(self.client.options["servers"]), 3)
        self.assertEqual(self.client.options["max_reconnect_attempts"], -1)
        self.assertTrue(self.client.options["allow_reconnect"])
        await self.client.options["disconnected_cb"]()
        self.assertEqual(rpc.state, "disconnected")
        await self.client.options["reconnected_cb"]()
        self.assertEqual(rpc.state, "connected")
        await rpc.close()
        self.assertEqual(rpc.state, "closed")

    async def test_failed_initial_connection_retries_until_cancelled(self):
        rpc = self.rpc()
        attempts = asyncio.Event()
        async def failed(**options):
            attempts.set()
            raise RuntimeError("private-value")
        self.client.connect = failed
        rpc.start()
        await attempts.wait()
        await asyncio.sleep(0)
        self.assertFalse(rpc.startup.done())
        self.assertEqual(rpc.state, "unavailable")
        await rpc.close()
        self.assertTrue(rpc.startup.cancelled())

    async def test_failed_drain_and_close_do_not_prevent_core_cleanup(self):
        rpc = self.rpc()
        async def failed():
            raise RuntimeError("private-value")
        self.client.drain = self.client.close = failed
        await rpc.close()
        self.assertEqual(rpc.state, "closed")


if __name__ == "__main__":
    unittest.main()
