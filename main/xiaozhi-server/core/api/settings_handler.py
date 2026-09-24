import asyncio
import ipaddress
import os

from aiohttp import web

from config.config_loader import get_project_dir
from core.api.base_handler import BaseHandler
from core.utils.config_editor import ConfigEditor
from core.utils.resource_monitor import ResourceMonitor
from core.utils.runtime_diagnostics import runtime_diagnostics


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
            "server.settings.allow_remote in data/.config.yaml to enable LAN access."
        )

    def _require_json(self, request):
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="Expected application/json")

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
            "resources.js",
            "shared.js",
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

    async def handle_put(self, request):
        self._require_access(request)
        self._require_json(request)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Request body must be an object")
            patch = body.get("config")
            payload = self.editor.update(patch)
        except (ValueError, TypeError) as error:
            return web.json_response({"error": str(error)}, status=400)

        self.restart_required = (
            self.restart_required or self._patch_requires_restart(patch)
        )
        payload["restart_required"] = self.restart_required
        return self._disable_cache(web.json_response(payload))

    async def handle_restart(self, request):
        self._require_access(request)
        self._require_json(request)
        asyncio.get_running_loop().call_later(0.5, self.request_restart)
        return web.json_response({"restarting": True})
