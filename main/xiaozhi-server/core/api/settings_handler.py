import asyncio
import ipaddress
import os
import re

from aiohttp import web

from config.config_loader import get_project_dir
from core.api.base_handler import BaseHandler
from core.notification_audio import (
    PRESENTATION_FIELDS,
    PushTtsError,
    TemporaryNotificationAudioService,
)
from core.soundbank import SoundbankAuthoringService, SoundbankError
from core.soundbank_cleanup import SoundbankCleanup
from core.utils.config_editor import ConfigEditor
from core.utils.resource_monitor import ResourceMonitor
from core.utils.runtime_diagnostics import runtime_diagnostics
from core.utils.ui_log_buffer import ui_log_buffer


class SettingsHandler(BaseHandler):
    def __init__(self, config, request_restart):
        super().__init__(config)
        self.request_restart = request_restart
        self.editor = ConfigEditor()
        self.resource_monitor = ResourceMonitor()
        self.web_dir = os.path.join(get_project_dir(), "web", "settings")
        settings_config = config.get("server", {}).get("settings", {})
        self.allow_remote = bool(settings_config.get("allow_remote", False))
        self.restart_required = False
        self.notification_audio = TemporaryNotificationAudioService(config)
        self.soundbank = SoundbankAuthoringService(config)
        self.soundbank_cleanup = SoundbankCleanup(config)
        try:
            result = self.editor.cleanup_soundbank(self.soundbank_cleanup)
            if result["deleted"] or result["errors"]:
                self.logger.info(f"Soundbank startup cleanup: {result}")
        except (OSError, ValueError, SoundbankError) as error:
            self.logger.warning(f"Soundbank startup cleanup deferred: {error}")

    @staticmethod
    def _disable_cache(response):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response

    def _require_access(self, request):
        if self.allow_remote:
            return
        peer = request.transport.get_extra_info("peername") if request.transport else None
        host = peer[0] if peer else ""
        try:
            address = ipaddress.ip_address(host)
            mapped_address = getattr(address, "ipv4_mapped", None)
            if address.is_loopback or (
                mapped_address is not None and mapped_address.is_loopback
            ):
                return
        except ValueError:
            pass
        raise web.HTTPForbidden(
            text="Settings UI only accepts local requests. Set "
            "server.settings.allow_remote in your local runtime config to enable LAN access."
        )

    def _require_json(self, request):
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="Expected application/json")

    @staticmethod
    def _soundbank_draft_id(body):
        value = body.get("soundbank_draft_id")
        if value is not None and (
            not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", value)
        ):
            raise ValueError("Invalid soundbank draft ID")
        return value

    @staticmethod
    def _patch_requires_restart(patch):
        if not isinstance(patch, dict) or set(patch) != {"server"}:
            return True
        server_patch = patch.get("server")
        if not isinstance(server_patch, dict) or set(server_patch) != {"settings"}:
            return True
        settings_patch = server_patch.get("settings")
        if not isinstance(settings_patch, dict) or set(settings_patch) != {
            "diagnostics"
        }:
            return True
        diagnostics_patch = settings_patch.get("diagnostics")
        return not (
            isinstance(diagnostics_patch, dict)
            and set(diagnostics_patch) == {"thresholds_ms"}
        )

    async def handle_index(self, request):
        self._require_access(request)
        return self._disable_cache(
            web.FileResponse(os.path.join(self.web_dir, "index.html"))
        )

    async def handle_redirect(self, request):
        self._require_access(request)
        raise web.HTTPFound("/settings/")

    async def handle_asset(self, request):
        self._require_access(request)
        filename = request.match_info["filename"]
        if filename not in {
            "app.js",
            "configuration.js",
            "diagnostics.js",
            "favicon.svg",
            "memory.js",
            "push_tts.js",
            "resources.js",
            "shared.js",
            "soundbank.js",
            "logs.js",
            "styles.css",
        }:
            raise web.HTTPNotFound()
        return self._disable_cache(
            web.FileResponse(os.path.join(self.web_dir, filename))
        )

    async def handle_get(self, request):
        self._require_access(request)
        payload = self.editor.read_public()
        payload["restart_required"] = self.restart_required
        payload["soundbank_runtime_directory"] = self.soundbank.runtime_directory()
        payload["soundbank_runtime_audio"] = self.soundbank.runtime_audio_contract()
        return self._disable_cache(web.json_response(payload))

    async def handle_status(self, request):
        self._require_access(request)
        scope = request.query.get("scope", "all")
        if scope not in {"all", "overview", "diagnostics"}:
            return self._disable_cache(
                web.json_response(
                    {"error": f"Unsupported status scope: {scope}"},
                    status=400,
                )
            )
        if scope == "diagnostics":
            payload = {
                "available": True,
                "runtime": runtime_diagnostics.snapshot(),
            }
        else:
            payload = await asyncio.to_thread(self.resource_monitor.sample)
            payload["runtime"] = runtime_diagnostics.snapshot(
                include_history=scope == "all"
            )
        return self._disable_cache(web.json_response(payload))

    async def handle_logs(self, request):
        self._require_access(request)
        payload = ui_log_buffer.snapshot(
            after=request.query.get("after", 0),
            limit=request.query.get("limit", 300),
        )
        return self._disable_cache(web.json_response(payload))

    async def handle_put(self, request):
        self._require_access(request)
        self._require_json(request)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Request body must be an object")
            patch = body.get("config")
            draft_id = self._soundbank_draft_id(body)
            retired = body.get("soundbank_retired_drafts", [])
            if not isinstance(retired, list) or len(retired) > 4096 or any(
                not isinstance(filename, str) for filename in retired
            ):
                raise ValueError("Invalid retired soundbank drafts")
            payload = await asyncio.to_thread(
                self.editor.update, patch, self.soundbank_cleanup, draft_id, retired
            )
        except (ValueError, TypeError) as error:
            return web.json_response({"error": str(error)}, status=400)

        self.restart_required = (
            self.restart_required or self._patch_requires_restart(patch)
        )
        payload["restart_required"] = self.restart_required
        payload["soundbank_runtime_directory"] = self.soundbank.runtime_directory()
        payload["soundbank_runtime_audio"] = self.soundbank.runtime_audio_contract()
        return self._disable_cache(web.json_response(payload))

    async def handle_restart(self, request):
        self._require_access(request)
        self._require_json(request)
        asyncio.get_running_loop().call_later(0.5, self.request_restart)
        return web.json_response({"restarting": True})

    async def handle_push_tts(self, request):
        self._require_access(request)
        self._require_json(request)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise PushTtsError("Request body must be an object")
            presentation = {
                key: body[key]
                for key in PRESENTATION_FIELDS
                if key in body
            }
            result = await self.notification_audio.push_tts(
                body.get("device_id"), body.get("text"), presentation
            )
        except PushTtsError as error:
            return self._disable_cache(
                web.json_response({"error": str(error)}, status=error.status)
            )
        except (ValueError, TypeError) as error:
            return self._disable_cache(
                web.json_response({"error": str(error)}, status=400)
            )

        return self._disable_cache(
            web.json_response(
                {
                    "success": True,
                    "device_id": result["device_id"],
                    "audio_url": result["audio_url"],
                    "message": "Audio generated and notify pushed; playback is not confirmed.",
                }
            )
        )

    async def handle_notify_audio(self, request):
        # Deliberately not protected by the Settings localhost policy: the
        # selected ESP32 must be able to download this opaque, temporary URL.
        path = self.notification_audio.audio_path(request.match_info["token"])
        if path is None:
            raise web.HTTPNotFound(text="Notification audio not found or expired")
        response = web.FileResponse(path)
        response.content_type = "audio/ogg"
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    async def handle_soundbank_generate(self, request):
        self._require_access(request)
        self._require_json(request)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise SoundbankError("Request body must be an object")
            if body.get("mode", "current") != "current":
                raise SoundbankError("only current TTS generation is supported")
            entry = await asyncio.to_thread(
                self._generate_soundbank,
                body.get("text"),
                body.get("title"),
                self._soundbank_draft_id(body),
            )
        except SoundbankError as error:
            return self._disable_cache(
                web.json_response({"error": str(error)}, status=error.status)
            )
        except (ValueError, TypeError) as error:
            return self._disable_cache(
                web.json_response({"error": str(error)}, status=400)
            )

        return self._disable_cache(
            web.json_response({"success": True, "entry": entry})
        )

    async def handle_soundbank_optimize(self, request):
        self._require_access(request)
        self._require_json(request)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise SoundbankError("Request body must be an object")
            optimized = await asyncio.to_thread(
                self._optimize_soundbank,
                body.get("file"),
                self._soundbank_draft_id(body),
            )
        except SoundbankError as error:
            return self._disable_cache(
                web.json_response({"error": str(error)}, status=error.status)
            )
        except (ValueError, TypeError) as error:
            return self._disable_cache(
                web.json_response({"error": str(error)}, status=400)
            )

        return self._disable_cache(
            web.json_response(
                {"success": True, "optimized": optimized}
            )
        )

    def _generate_soundbank(self, text, title, draft_id):
        with self.soundbank_cleanup.lock:
            entry = self.soundbank.generate(text, title)
            self.soundbank_cleanup.protect_draft(entry, draft_id)
            return entry

    def _optimize_soundbank(self, filename, draft_id):
        with self.soundbank_cleanup.lock:
            optimized = self.soundbank.optimize(filename)
            self.soundbank_cleanup.protect_draft(
                {"file": filename, "optimized": optimized}, draft_id, owned_canonical=False
            )
            return optimized

    async def handle_soundbank_cleanup(self, request):
        self._require_access(request)
        self._require_json(request)
        try:
            body = await request.json()
            if not isinstance(body, dict) or body.get("action") not in ("preview", "delete"):
                raise SoundbankError("Cleanup action must be preview or delete")
            token = None
            if body["action"] == "delete":
                token = body.get("token")
                if not isinstance(token, str) or len(token) != 64:
                    raise SoundbankError("Preview cleanup before deleting unused sounds")
            result = await asyncio.to_thread(
                self.editor.cleanup_soundbank, self.soundbank_cleanup, True, token
            )
        except (OSError, ValueError, SoundbankError) as error:
            return self._disable_cache(web.json_response(
                {"error": str(error)}, status=getattr(error, "status", 400)
            ))
        return self._disable_cache(web.json_response(result))

    async def handle_soundbank_audio(self, request):
        self._require_access(request)
        try:
            content, content_type = await asyncio.to_thread(
                self.soundbank.preview_audio,
                request.match_info.get("filename", ""),
            )
        except SoundbankError as error:
            return self._disable_cache(
                web.json_response({"error": str(error)}, status=error.status)
            )

        if isinstance(content, bytes):
            response = web.Response(body=content, content_type=content_type)
        else:
            response = web.FileResponse(content)
            response.content_type = content_type
        return self._disable_cache(response)

    def close(self):
        self.notification_audio.close()
