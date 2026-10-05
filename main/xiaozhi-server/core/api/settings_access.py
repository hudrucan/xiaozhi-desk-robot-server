"""Settings access/static routes with no conversation runtime dependencies."""

import ipaddress
import os

from aiohttp import web


class SettingsAccess:
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
            if address.is_loopback or (mapped_address is not None and mapped_address.is_loopback):
                return
        except ValueError:
            pass
        raise web.HTTPForbidden(text=self.access_error)

    access_error = (
        "Settings UI only accepts local requests. Set "
        "server.settings.allow_remote in your local runtime config to enable LAN access."
    )

    def _require_json(self, request):
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="Expected application/json")

    async def handle_index(self, request):
        self._require_access(request)
        return self._disable_cache(web.FileResponse(os.path.join(self.web_dir, "index.html")))

    async def handle_redirect(self, request):
        self._require_access(request)
        raise web.HTTPFound("/settings/")

    async def handle_asset(self, request):
        self._require_access(request)
        filename = request.match_info["filename"]
        if filename not in {
            "app.js", "configuration.js", "cluster.js", "diagnostics.js", "favicon.svg",
            "memory.js", "push_tts.js", "resources.js", "shared.js", "soundbank.js",
            "logs.js", "styles.css",
        }:
            raise web.HTTPNotFound()
        return self._disable_cache(web.FileResponse(os.path.join(self.web_dir, filename)))
