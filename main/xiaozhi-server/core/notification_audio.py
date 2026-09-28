"""Temporary Push TTS audio generation for connected Desk Robots."""

import asyncio
import ipaddress
import os
from pathlib import Path
import re
import secrets
import tempfile

from config.logger import setup_logging
from core.connected_devices import connected_devices
from core.utils.util import get_local_ip


TAG = __name__
MAX_TEXT_LENGTH = 1000
DEFAULT_AUDIO_TTL_SECONDS = 600
_SAFE_EXTENSION = re.compile(r"^[a-z0-9]{1,8}$")


class PushTtsError(Exception):
    """A user-facing Push TTS failure with an appropriate HTTP status."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class TemporaryNotificationAudioService:
    """Generate short-lived Ogg/Opus files and push firmware notify messages."""

    def __init__(self, config, ttl_seconds=DEFAULT_AUDIO_TTL_SECONDS):
        self.config = config
        self.ttl_seconds = max(30, int(ttl_seconds))
        self.logger = setup_logging()
        self._temporary_directory = tempfile.TemporaryDirectory(
            prefix="xiaozhi-notify-"
        )
        self._directory = Path(self._temporary_directory.name)
        self._audio_files = {}

    @staticmethod
    def validate_input(device_id, text):
        if not isinstance(device_id, str) or not device_id.strip():
            raise PushTtsError("device_id must be a non-empty string")
        if len(device_id) > 128:
            raise PushTtsError("device_id is too long")
        if not isinstance(text, str) or not text.strip():
            raise PushTtsError("text must be a non-empty string")
        normalized_text = text.strip()
        if len(normalized_text) > MAX_TEXT_LENGTH:
            raise PushTtsError(
                f"text must not exceed {MAX_TEXT_LENGTH} characters"
            )
        return device_id.strip(), normalized_text

    async def push_tts(self, device_id, text):
        device_id, text = self.validate_input(device_id, text)
        handler = await connected_devices.get(device_id)
        if handler is None or handler.stop_event.is_set():
            raise PushTtsError("The selected robot is not connected", status=404)
        if not handler.persistent_websocket:
            raise PushTtsError(
                "Push TTS requires a persistent WebSocket connection",
                status=409,
            )
        if handler.client_listening or handler.has_active_turn_metrics():
            raise PushTtsError(
                "The selected robot is busy with an active conversation",
                status=409,
            )
        if handler.tts is None:
            raise PushTtsError(
                "The selected robot's TTS provider is not ready",
                status=409,
            )

        token = await self._generate_ogg(handler.tts, text)
        try:
            audio_url = self._build_audio_url(handler, token)
            payload = {
                "type": "notify",
                "audio_url": audio_url,
                "subtitles": [{"start_ms": 0, "text": text}],
            }
            # Deliver through the registry only if this is still the exact
            # persistent handler whose active TTS provider generated the file.
            if not await connected_devices.send_if_current(
                device_id, handler, payload
            ):
                raise PushTtsError(
                    "Audio was generated, but the robot connection was lost before push",
                    status=409,
                )
        except Exception:
            self.discard(token)
            raise

        self.logger.bind(tag=TAG).info(
            f"Push TTS notify sent to device {device_id}"
        )
        return {"device_id": device_id, "audio_url": audio_url}

    async def _generate_ogg(self, tts_provider, text):
        extension = str(getattr(tts_provider, "audio_file_type", "bin")).lower()
        if not _SAFE_EXTENSION.fullmatch(extension):
            extension = "bin"

        token = secrets.token_urlsafe(24)
        source_path = self._directory / f"source-{token}.{extension}"
        output_path = self._directory / f"{token}.ogg"
        completed = False
        try:
            result = await asyncio.to_thread(
                self._run_tts_generation,
                tts_provider,
                text,
                source_path,
            )
            if result and (
                not source_path.exists() or source_path.stat().st_size == 0
            ):
                source_path.write_bytes(result)
            if not source_path.is_file() or source_path.stat().st_size == 0:
                raise PushTtsError("TTS provider returned no audio", status=502)

            process = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-nostdin",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                os.fspath(source_path),
                "-vn",
                "-ac",
                "1",
                "-c:a",
                "libopus",
                "-f",
                "ogg",
                os.fspath(output_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate()
            if process.returncode != 0:
                detail = stderr.decode("utf-8", errors="replace").strip()
                raise PushTtsError(
                    f"ffmpeg could not convert generated TTS audio: {detail[:300]}",
                    status=502,
                )
            if not output_path.is_file() or output_path.stat().st_size == 0:
                raise PushTtsError(
                    "ffmpeg produced an empty notification audio file",
                    status=502,
                )
            completed = True
        except FileNotFoundError as error:
            raise PushTtsError(
                "ffmpeg is not installed or executable", status=500
            ) from error
        except PushTtsError:
            raise
        except Exception as error:
            raise PushTtsError(f"TTS generation failed: {error}", status=502) from error
        finally:
            source_path.unlink(missing_ok=True)
            if not completed:
                output_path.unlink(missing_ok=True)

        timer = asyncio.get_running_loop().call_later(
            self.ttl_seconds, self.discard, token
        )
        self._audio_files[token] = (output_path, timer)
        return token

    @staticmethod
    def _run_tts_generation(tts_provider, text, source_path):
        return asyncio.run(tts_provider.text_to_speak(text, os.fspath(source_path)))

    def _build_audio_url(self, handler, token):
        server_config = self.config.get("server", {})
        listen_host = str(server_config.get("ip", "0.0.0.0")).strip()
        if self._is_loopback_host(listen_host):
            raise PushTtsError(
                "HTTP server is bound to loopback and is not reachable by the robot",
                status=409,
            )

        candidates = []
        websocket = getattr(handler, "websocket", None)
        local_address = getattr(websocket, "local_address", None)
        if isinstance(local_address, (tuple, list)) and local_address:
            candidates.append(local_address[0])
        transport = getattr(websocket, "transport", None)
        if transport is not None:
            socket_name = transport.get_extra_info("sockname")
            if isinstance(socket_name, (tuple, list)) and socket_name:
                candidates.append(socket_name[0])
        if listen_host not in {"0.0.0.0", "::", ""}:
            candidates.append(listen_host)
        candidates.append(get_local_ip())

        host = None
        for candidate in candidates:
            host = self._format_reachable_host(candidate)
            if host is not None:
                break
        if host is None:
            raise PushTtsError(
                "Could not resolve a non-loopback HTTP address reachable by the robot",
                status=409,
            )
        port = int(server_config.get("http_port", 8003))
        if not 1 <= port <= 65535:
            raise PushTtsError(
                "HTTP server port is not available for notification audio",
                status=409,
            )
        return f"http://{host}:{port}/api/notify/audio/{token}.ogg"

    @staticmethod
    def _is_loopback_host(host):
        if host.lower() == "localhost":
            return True
        try:
            return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
        except ValueError:
            return False

    @staticmethod
    def _format_reachable_host(host):
        if not isinstance(host, str) or not host:
            return None
        clean_host = host.strip().strip("[]")
        if not clean_host or clean_host.lower() == "localhost":
            return None
        try:
            address = ipaddress.ip_address(clean_host.split("%", 1)[0])
        except ValueError:
            return clean_host
        if address.is_loopback or address.is_unspecified:
            return None
        return f"[{clean_host}]" if address.version == 6 else clean_host

    def audio_path(self, token):
        if not isinstance(token, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{32}", token
        ):
            return None
        record = self._audio_files.get(token)
        if record is None:
            return None
        path, _ = record
        return path if path.is_file() else None

    def discard(self, token):
        record = self._audio_files.pop(token, None)
        if record is None:
            return
        path, timer = record
        if timer is not None and not timer.cancelled():
            timer.cancel()
        path.unlink(missing_ok=True)

    def close(self):
        for token in list(self._audio_files):
            self.discard(token)
        self._temporary_directory.cleanup()
