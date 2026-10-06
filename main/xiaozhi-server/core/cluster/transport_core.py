"""Bounded, authenticated gateway transport sessions without provider imports."""

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import WSMsgType, web

from config.node_identity import hostname_node_id

LOGGER = logging.getLogger("xiaozhi.core")
PROTOCOL = "xiaozhi-core-transport-v1"
MAX_JSON = 8192
MAX_FRAME = 65507
MAX_SESSIONS = 128


@dataclass(frozen=True)
class CoreConfig:
    node_id: str
    host: str
    port: int
    gateway_ips: frozenset[str]
    management_interface: str
    ingress_state_file: str
    secret: str = field(repr=False)

    @classmethod
    def from_env(cls):
        node = os.environ.get("XIAOZHI_CORE_ID", hostname_node_id())
        host = os.environ.get("XIAOZHI_CORE_HOST", "127.0.0.1")
        address = ipaddress.IPv4Address(host)
        port = int(os.environ.get("XIAOZHI_CORE_PORT", "8000"))
        secret = os.environ.get("XIAOZHI_CORE_AUTH_KEY", "")
        interface = os.environ.get("XIAOZHI_CORE_MANAGEMENT_INTERFACE", "")
        path = os.environ.get("XIAOZHI_CORE_INGRESS_STATE_FILE", "/etc/xiaozhi-ingress.json")
        entries = os.environ.get("XIAOZHI_CORE_GATEWAY_IPS", "").split(",")
        ips = frozenset(str(ipaddress.IPv4Address(v.strip())) for v in entries)
        if (not re.fullmatch(r"[A-Za-z0-9_-]{1,192}", node)
                or not address.is_private or address.is_unspecified or address.is_multicast
                or not 1024 <= port <= 65535 or not secret.strip()
                or len(secret) > 4096 or any(c in secret for c in "\x00\r\n")
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", interface)
                or not Path(path).is_absolute() or not 1 <= len(ips) <= 3
                or any(not ipaddress.IPv4Address(v).is_private
                       or ipaddress.IPv4Address(v).is_unspecified
                       or ipaddress.IPv4Address(v).is_multicast for v in ips)):
            raise ValueError("Invalid core configuration")
        return cls(node, host, port, ips, interface, path, secret)


def authenticated(request, config):
    if request.remote not in config.gateway_ips:
        return False
    mac = request.headers.get("device-id", "")
    client = request.headers.get("client-id", "default-client-id")
    token = request.headers.get("authorization", "")
    if (not re.fullmatch(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}", mac)
            or not 1 <= len(client) <= 512 or any(ord(c) < 32 for c in client)
            or len(token) > 128 or not token.startswith("Bearer ")):
        return False
    try:
        signature, stamp = token[7:].rsplit(".", 1)
        if not re.fullmatch(r"[0-9]{1,12}", stamp) or abs(time.time() - int(stamp)) > 300:
            return False
        digest = hmac.new(config.secret.encode(), f"{client}|{mac}|{stamp}".encode(), hashlib.sha256).digest()
        expected = base64.urlsafe_b64encode(digest).decode().rstrip("=")
        return hmac.compare_digest(signature, expected)
    except (ValueError, TypeError):
        return False


def hello_audio(message):
    audio = message.get("audio_params")
    if (message.get("type") != "hello" or type(message.get("version")) is not int
            or message["version"] != 2 or message.get("transport") != "websocket"
            or not isinstance(audio, dict) or audio.get("format") != "opus"
            or type(audio.get("sample_rate")) is not int or not 8000 <= audio["sample_rate"] <= 48000
            or type(audio.get("channels")) is not int or audio["channels"] not in (1, 2)
            or type(audio.get("frame_duration")) not in (int, float)
            or not math.isfinite(audio["frame_duration"]) or not 0 < audio["frame_duration"] <= 120
            or ("features" in message and not isinstance(message["features"], dict))):
        raise ValueError("Invalid hello")
    return {key: audio[key] for key in ("format", "sample_rate", "channels", "frame_duration")}


def decode_message(data):
    if len(data.encode("utf-8")) > MAX_JSON:
        raise ValueError("Oversize JSON")
    def invalid_constant(value):
        raise ValueError("Invalid JSON constant")
    message = json.loads(data, parse_constant=invalid_constant)
    if (not isinstance(message, dict) or not isinstance(message.get("type"), str)
            or not 1 <= len(message["type"]) <= 64):
        raise ValueError("Invalid message")
    return message


class TransportCore:
    def __init__(self, config):
        self.config = config
        self.sockets = set()
        self.sessions = set()
        self.received_audio_frames = 0
        self.vip_owner = None
        self.stopping = False
        self.runner = None
        self.vip_task = None

    async def refresh_vip(self):
        while True:
            owner = None
            try:
                def read_snapshot():
                    with open(self.config.ingress_state_file, "rb") as stream:
                        return stream.read(MAX_JSON + 1)
                raw = await asyncio.to_thread(read_snapshot)
                if len(raw) > MAX_JSON:
                    raise ValueError("Oversize ingress snapshot")
                configured = json.loads(raw)["vip"]
                if not isinstance(configured, str):
                    raise ValueError("Invalid ingress snapshot")
                address = ipaddress.IPv4Address(configured)
                if address.is_unspecified or address.is_multicast:
                    raise ValueError("Invalid ingress snapshot")
                vip = str(address)
                process = await asyncio.create_subprocess_exec(
                    "/usr/sbin/ip", "-j", "-4", "address", "show", "dev", self.config.management_interface,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                try:
                    output, _ = await asyncio.wait_for(process.communicate(), timeout=1)
                finally:
                    if process.returncode is None:
                        process.kill()
                    await process.wait()
                if process.returncode or len(output) > MAX_JSON:
                    raise ValueError("Unavailable interface")
                owner = any(a.get("family") == "inet" and a.get("local") == vip
                            for interface in json.loads(output)
                            if interface.get("ifname") == self.config.management_interface
                            for a in interface.get("addr_info", []))
            except (OSError, ValueError, KeyError, TypeError, AttributeError,
                    RecursionError, asyncio.TimeoutError):
                pass
            self.vip_owner = owner
            await asyncio.sleep(1)

    async def status(self, request):
        return web.json_response({"protocol": PROTOCOL, "core_id": self.config.node_id,
            "status": "stopping" if self.stopping else "ready",
            "active_sessions": len(self.sessions), "local_vip_owner": self.vip_owner,
            "received_audio_frames": self.received_audio_frames,
            "capabilities": [], "conversation_runtime": False})

    async def ready(self, request):
        return web.json_response({"status": "not_ready" if self.stopping else "ready",
            "protocol": PROTOCOL, "conversation_runtime": False}, status=503 if self.stopping else 200)

    async def websocket(self, request):
        if request.query_string != "from=mqtt_gateway" or not authenticated(request, self.config):
            raise web.HTTPUnauthorized()
        if self.stopping or len(self.sockets) >= MAX_SESSIONS:
            raise web.HTTPServiceUnavailable()
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=MAX_FRAME, compress=False)
        # Reserve admission before the upgrade await, including incomplete hellos.
        self.sockets.add(ws)
        session = None
        unavailable_sent = False
        try:
            await ws.prepare(request)
            first = await asyncio.wait_for(ws.receive(), timeout=3)
            if first.type != WSMsgType.TEXT:
                raise ValueError("Hello required")
            audio = hello_audio(decode_message(first.data))
            session = uuid.uuid4().hex
            await ws.send_json({"type": "hello", "version": 2, "transport": "websocket",
                "session_id": session, "audio_params": audio, "core_id": self.config.node_id,
                "capabilities": [], "conversation_runtime": False})
            self.sessions.add(session)
            LOGGER.info("core_id=%s session opened active=%d", self.config.node_id, len(self.sessions))
            async for frame in ws:
                if frame.type == WSMsgType.BINARY:
                    data = frame.data
                    if (len(data) < 17 or data[:8] != bytes(8)
                            or not 0 < int.from_bytes(data[12:16], "big") == len(data) - 16):
                        raise ValueError("Invalid audio frame")
                    self.received_audio_frames += 1
                elif frame.type == WSMsgType.TEXT:
                    message = decode_message(frame.data)
                    if message["type"] == "goodbye":
                        break
                    if message["type"] == "hello":
                        raise ValueError("Duplicate hello")
                    if message["type"] == "ping":
                        await ws.send_json({"type": "pong", "session_id": session})
                    elif message["type"] == "abort":
                        # No provider job exists in this transport-only phase.
                        unavailable_sent = False
                    elif not unavailable_sent:
                        await ws.send_json({"type": "error", "session_id": session,
                            "code": "conversation_runtime_unavailable",
                            "message": "This core supports transport probes only; provider execution is not enabled"})
                        unavailable_sent = True
                elif frame.type in (WSMsgType.ERROR, WSMsgType.CLOSE, WSMsgType.CLOSED):
                    break
        except (ValueError, TypeError, KeyError, RecursionError, asyncio.TimeoutError):
            if ws.prepared:
                await ws.close(code=1008, message=b"Invalid transport message")
        finally:
            self.sockets.discard(ws)
            if session is not None:
                self.sessions.discard(session)
                LOGGER.info("core_id=%s session closed active=%d", self.config.node_id, len(self.sessions))
            if ws.prepared:
                await ws.close()
        return ws

    async def start(self):
        app = web.Application(client_max_size=MAX_JSON)
        app.router.add_get("/xiaozhi/v1/", self.websocket)
        app.router.add_get("/status", self.status)
        app.router.add_get("/readyz", self.ready)
        self.runner = web.AppRunner(app, access_log=None, shutdown_timeout=5)
        await self.runner.setup()
        await web.TCPSite(self.runner, self.config.host, self.config.port).start()
        self.vip_task = asyncio.create_task(self.refresh_vip())
        LOGGER.info("core_id=%s transport ready; provider execution disabled", self.config.node_id)

    async def close(self):
        self.stopping = True
        if self.vip_task:
            self.vip_task.cancel()
            await asyncio.gather(self.vip_task, return_exceptions=True)
        try:
            await asyncio.wait_for(asyncio.gather(
                *(ws.close(code=1001, message=b"Core shutting down") for ws in list(self.sockets)),
                return_exceptions=True), timeout=5)
        except asyncio.TimeoutError:
            pass
        if self.runner:
            await self.runner.cleanup()
