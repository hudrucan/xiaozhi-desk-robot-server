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
from .voice_diagnostics import VoiceDiagnostics, PROTOCOL as DIAGNOSTIC_PROTOCOL, STATES

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
    def __init__(self, config, worker_rpc=None, voice_revision=None, tts_bundle=None):
        self.config = config
        self.worker_rpc = worker_rpc
        if voice_revision is not None and (type(voice_revision) is not int or voice_revision < 1 or worker_rpc is None):
            raise ValueError('Voice runtime requires an explicit revision and worker RPC')
        self.voice_revision = voice_revision
        self.tts_pool = None
        if tts_bundle is not None:
            if voice_revision is None or tts_bundle['revision'] != voice_revision:
                raise ValueError('TTS requires matching opt-in voice revision')
            from .tts_client import TTSPool
            self.tts_pool = TTSPool(worker_rpc, tts_bundle)
        self.sockets = set()
        self.sessions = set()
        self.voices = {}
        self.mcps = {}
        self.diagnostics = VoiceDiagnostics()
        self.received_audio_frames = 0
        self.vip_owner = None
        self.stopping = False
        self.runner = None
        self.vip_task = None

    async def vision_capability(self, session):
        if self.tts_pool is None or not self.tts_pool.bundle.get('vision_enabled', False):
            return None
        try:
            def read():
                with open(self.config.ingress_state_file, 'rb') as stream:
                    raw = stream.read(MAX_JSON + 1)
                if len(raw) > MAX_JSON:
                    raise ValueError('Invalid ingress snapshot')
                vip = ipaddress.IPv4Address(json.loads(raw)['vip'])
                if not vip.is_private or vip.is_unspecified or vip.is_multicast or vip.is_loopback:
                    raise ValueError('Invalid ingress VIP')
                return str(vip)
            vip = await asyncio.to_thread(read)
            return {'url':f'http://{vip}/mcp/vision/explain',
                    'token':f'{self.config.node_id}.{session}.{uuid.uuid4().hex}'}
        except (OSError, ValueError, TypeError, KeyError):
            return None

    async def vision_upload(self, request):
        from .vision_http import upload
        return await upload(request, self)

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
            "capabilities": (['voice_text', 'voice_tts'] if self.tts_pool else ['voice_text']) if self.voice_revision is not None else [], "conversation_runtime": False,
            "tts": self.tts_pool.status() if self.tts_pool else {'enabled': False},
            "vision": {'enabled': bool(self.tts_pool and self.tts_pool.bundle.get('vision_enabled', False))},
            "worker_rpc": self.worker_rpc.status() if self.worker_rpc else {"state": "disabled"}})

    async def voice_diagnostics(self, request):
        if request.remote not in self.config.gateway_ips or request.query_string:
            raise web.HTTPForbidden()
        counts = {state: 0 for state in STATES}
        for voice in self.voices.values():
            counts[voice.state] += 1
        return web.json_response({'protocol': DIAGNOSTIC_PROTOCOL, 'node_id': self.config.node_id,
            'sessions': counts, 'events': self.diagnostics.snapshot(), 'mcp': any(mcp.valid for mcp in self.mcps.values())},
            headers={'Cache-Control': 'no-store'})

    async def worker_probe(self, request):
        # A private deployment diagnostic; never mounted in Settings or exposed
        # through the management VIP frontend.
        if request.query_string or not authenticated(request, self.config):
            raise web.HTTPUnauthorized()
        if self.stopping or self.worker_rpc is None:
            return web.json_response({"error": "worker_rpc_unavailable"}, status=503)
        try:
            raw = await asyncio.wait_for(request.read(), timeout=2)
            if len(raw) > 512:
                raise ValueError("Oversize probe")
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) - {"worker_id"}:
                raise ValueError("Invalid probe")
            worker_id = value.get("worker_id")
            if "worker_id" in value and not isinstance(worker_id, str):
                raise ValueError("Invalid worker identity")
            from .worker_rpc import WorkerRpcError
            try:
                reply = await self.worker_rpc.ping(worker_id)
            except WorkerRpcError as error:
                return web.json_response({"error": error.code}, status=503)
            return web.json_response({"protocol": "xiaozhi-core-worker-rpc-v1",
                                      "core_id": self.config.node_id, "worker": reply})
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return web.json_response({"error": "invalid_worker_probe"}, status=400)
        except asyncio.TimeoutError:
            return web.json_response({"error": "worker_probe_timeout"}, status=408)

    async def worker_llm_probe(self, request):
        if request.query_string or not authenticated(request, self.config):
            raise web.HTTPUnauthorized()
        if self.stopping or self.worker_rpc is None:
            return web.json_response({"error": "worker_rpc_unavailable"}, status=503)
        try:
            raw = await asyncio.wait_for(request.read(), timeout=2)
            from . import llm_protocol as protocol
            value = protocol.decode(raw, MAX_JSON)
            if not isinstance(value, dict) or set(value) - {"revision", "dialogue", "timeout_seconds"} or not {"revision", "dialogue"} <= set(value):
                raise ValueError("Invalid text probe")
            from .worker_rpc import WorkerRpcError
            try:
                reply = await self.worker_rpc.generate(value["revision"], value["dialogue"], value.get("timeout_seconds", 30))
            except WorkerRpcError as error:
                return web.json_response({"error": error.code}, status=503)
            return web.json_response(reply, status=200 if reply["status"] == "ok" else 503)
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            return web.json_response({"error": "invalid_llm_probe"}, status=400)
        except asyncio.TimeoutError:
            return web.json_response({"error": "worker_probe_timeout"}, status=408)

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
        voice = mcp = None
        try:
            await ws.prepare(request)
            first = await asyncio.wait_for(ws.receive(), timeout=3)
            if first.type != WSMsgType.TEXT:
                raise ValueError("Hello required")
            greeting = decode_message(first.data)
            audio = hello_audio(greeting)
            if self.voice_revision is not None and audio != {'format':'opus','sample_rate':16000,'channels':1,'frame_duration':60}:
                raise ValueError('Voice worker requires 16kHz mono 60ms Opus')
            session = uuid.uuid4().hex
            if self.voice_revision is not None:
                from .voice_turn import VoiceTurn
                if greeting.get('features', {}).get('mcp') is True:
                    from .device_mcp import DeviceMCP
                    mcp = DeviceMCP(ws.send_json, lambda event, **fields: self.diagnostics.record(event, session, **fields),
                        vision=await self.vision_capability(session),
                        device_id=request.headers.get('device-id', '').lower(),
                        client_id=request.headers.get('client-id', 'default-client-id'))
                    self.mcps[session] = mcp
                voice = VoiceTurn(self.worker_rpc, self.voice_revision, audio, session, ws.send_json,
                    self.tts_pool, ws.send_bytes if self.tts_pool else None, self.diagnostics, mcp)
                self.voices[session] = voice
            await ws.send_json({"type": "hello", "version": 2, "transport": "websocket",
                "session_id": session, "audio_params": audio, "core_id": self.config.node_id,
                "capabilities": (['voice_text', 'voice_tts'] if self.tts_pool else ['voice_text']) if voice else [], "conversation_runtime": False})
            if mcp is not None:
                mcp.start()
            self.sessions.add(session)
            self.diagnostics.record('session_opened', session)
            LOGGER.info("core_id=%s session opened active=%d", self.config.node_id, len(self.sessions))
            async for frame in ws:
                if frame.type == WSMsgType.BINARY:
                    data = frame.data
                    if (len(data) < 17 or data[:8] != bytes(8)
                            or not 0 < int.from_bytes(data[12:16], "big") == len(data) - 16):
                        raise ValueError("Invalid audio frame")
                    self.received_audio_frames += 1
                    if voice:
                        await voice.audio_frame(data[16:])
                elif frame.type == WSMsgType.TEXT:
                    message = decode_message(frame.data)
                    if message["type"] == "goodbye":
                        break
                    if message["type"] == "hello":
                        raise ValueError("Duplicate hello")
                    if message["type"] == "ping":
                        await ws.send_json({"type": "pong", "session_id": session})
                    elif message["type"] == "abort":
                        if voice:
                            await voice.abort()
                            await voice.emit({'type': 'stt', 'state': 'clear'})
                        unavailable_sent = False
                    elif message['type'] == 'mcp':
                        if mcp is not None:
                            mcp.receive(message.get('payload'))
                    elif voice and message['type'] == 'listen':
                        if message.get('state') == 'start':
                            await voice.start(message.get('mode', 'auto'))
                        elif message.get('state') == 'stop':
                            await voice.stop()
                        elif message.get('state') == 'detect' and (message.get('input_mode') == 'text' or message.get('text') == 'web_chat'):
                            await voice.start_text(message.get('text'))
                        elif message.get('state') == 'detect' and message.get('input_mode') != 'text':
                            await voice.start_text(message.get('text'), wake=True)
                        else:
                            raise ValueError('Unsupported voice request')
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
            # This transport session is over even if owned provider cleanup
            # takes longer. Never advertise a disconnected session as active.
            if session is not None:
                self.voices.pop(session, None)
                self.mcps.pop(session, None)
                self.sessions.discard(session)
                self.diagnostics.record('session_closed', session)
                LOGGER.info("core_id=%s session closed active=%d", self.config.node_id, len(self.sessions))
            try:
                try:
                    if voice:
                        await voice.abort()
                finally:
                    if mcp:
                        await mcp.close()
            finally:
                # Retain the socket quota while its handler is cleaning up.
                self.sockets.discard(ws)
                if ws.prepared:
                    await ws.close()
        return ws

    async def start(self):
        from .vision_http import MAX_BODY
        app = web.Application(client_max_size=MAX_BODY)
        app.router.add_get("/xiaozhi/v1/", self.websocket)
        app.router.add_post('/mcp/vision/explain', self.vision_upload)
        app.router.add_get("/status", self.status)
        app.router.add_get("/diagnostics", self.voice_diagnostics)
        app.router.add_get("/readyz", self.ready)
        app.router.add_post("/api/workers/probe", self.worker_probe)
        app.router.add_post("/api/workers/llm", self.worker_llm_probe)
        self.runner = web.AppRunner(app, access_log=None, shutdown_timeout=5, handler_cancellation=True)
        await self.runner.setup()
        await web.TCPSite(self.runner, self.config.host, self.config.port).start()
        self.vip_task = asyncio.create_task(self.refresh_vip())
        if self.worker_rpc:
            self.worker_rpc.start()
        LOGGER.info("core_id=%s transport ready; voice_mode=%s", self.config.node_id,
            'voice_tts' if self.tts_pool else 'voice_text' if self.voice_revision is not None else 'disabled')

    async def close(self):
        self.stopping = True
        if self.worker_rpc:
            await self.worker_rpc.close()
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
