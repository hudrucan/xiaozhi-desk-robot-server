import asyncio
import ipaddress
import os

from aiohttp import web

from config.config_loader import get_project_dir
from core.api.base_handler import BaseHandler
from core.utils.config_editor import ConfigEditor


class SettingsHandler(BaseHandler):
    def __init__(self, config, request_restart):
        super().__init__(config)
        self.request_restart = request_restart
        self.editor = ConfigEditor()
        self.web_dir = os.path.join(get_project_dir(), "web", "settings")
        settings_config = config.get("server", {}).get("settings", {})
        self.allow_remote = bool(settings_config.get("allow_remote", False))
        self.restart_required = False

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

    async def handle_index(self, request):
        self._require_access(request)
        return web.FileResponse(os.path.join(self.web_dir, "index.html"))

    async def handle_redirect(self, request):
        self._require_access(request)
        raise web.HTTPFound("/settings/")

    async def handle_asset(self, request):
        self._require_access(request)
        filename = request.match_info["filename"]
        if filename not in {"app.js", "styles.css"}:
            raise web.HTTPNotFound()
        return web.FileResponse(os.path.join(self.web_dir, filename))

    async def handle_get(self, request):
        self._require_access(request)
        payload = self.editor.read_public()
        payload["restart_required"] = self.restart_required
        return web.json_response(payload)

    async def handle_put(self, request):
        self._require_access(request)
        self._require_json(request)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Request body must be an object")
            payload = self.editor.update(body.get("config"))
        except (ValueError, TypeError) as error:
            return web.json_response({"error": str(error)}, status=400)

        self.restart_required = True
        payload["restart_required"] = True
        return web.json_response(payload)

    async def handle_restart(self, request):
        self._require_access(request)
        self._require_json(request)
        asyncio.get_running_loop().call_later(0.5, self.request_restart)
        return web.json_response({"restarting": True})
