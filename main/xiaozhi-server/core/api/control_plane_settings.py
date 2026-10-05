"""Only desired-configuration Settings routes; no process-local runtime handlers."""

import asyncio
import copy
from pathlib import Path

from aiohttp import web

from config.config_loader import get_project_dir
from config.config_store import ConfigConflict, ConfigUnavailable
from core.api.settings_access import SettingsAccess
from core.cluster.config_reconciliation import CAPABILITIES, CONTROL_PROTOCOL, safe_source
from core.utils.config_editor import ConfigEditor


class ControlPlaneSettingsHandler(SettingsAccess):
    access_error = "Control-plane Settings requires loopback or explicit XIAOZHI_CONTROL_PLANE_ALLOW_REMOTE=true"

    def __init__(self, reconciliation):
        self.reconciliation = reconciliation
        self.editor = ConfigEditor(reconciliation.store)
        self.allow_remote = reconciliation.config.allow_remote
        self.web_dir = str(Path(get_project_dir()) / "web/settings")

    def capabilities(self):
        return {"protocol": CONTROL_PROTOCOL, "capability_version": 1,
                "mode": "standalone", "capabilities": copy.deepcopy(CAPABILITIES)}

    def _decorate(self, payload):
        # Preserve the response's revision/config pair, rather than borrowing a
        # later concurrent status snapshot for the browser's next CAS base.
        payload["configuration_source"] = safe_source(payload["configuration_source"])
        payload["config_path"] = "Shared Cloud desired configuration"
        payload["control_plane"] = self.capabilities()
        return payload

    def _error(self, error):
        if isinstance(error, ConfigConflict):
            status, code, message = 409, "config_conflict", "Cloud configuration changed; sync and review before retrying"
        elif isinstance(error, (ConfigUnavailable, OSError)):
            status, code, message = 503, "cloud_unavailable", "Validated Cloud configuration is unavailable; retry synchronization"
        else:
            status, code, message = 400, "invalid_config", "Invalid desired configuration; shared plaintext secrets are unsupported and blank fields must preserve existing references"
        return self._disable_cache(web.json_response({
            "error": message, "code": code,
            "configuration_source": copy.deepcopy(self.reconciliation.source),
            "control_plane": self.capabilities(),
        }, status=status))

    async def handle_index(self, request):
        self._require_access(request)
        content = await asyncio.to_thread((Path(self.web_dir) / "index.html").read_text, encoding="utf-8")
        # Decide mode before app.js can schedule runtime-only status polling.
        content = content.replace("<body>", '<body data-settings-mode="control-plane">', 1)
        return self._disable_cache(web.Response(text=content, content_type="text/html"))

    async def handle_capabilities(self, request):
        self._require_access(request)
        return self._disable_cache(web.json_response(self.capabilities()))

    async def handle_get(self, request):
        self._require_access(request)
        try:
            payload = await self.reconciliation.operation(self.editor.read_public)
        except (OSError, ValueError, TypeError, KeyError) as error:
            return self._error(error)
        return self._disable_cache(web.json_response(self._decorate(payload)))

    async def handle_put(self, request):
        self._require_access(request)
        self._require_json(request)
        try:
            body = await request.json()
            if not isinstance(body, dict) or body.get("soundbank_retired_drafts"):
                raise ValueError
            payload = await self.reconciliation.operation(
                self.editor.update, body.get("config"), base_revision=body.get("base_revision"),
            )
        except (OSError, ValueError, TypeError, KeyError) as error:
            return self._error(error)
        return self._disable_cache(web.json_response(self._decorate(payload)))

    async def handle_sync(self, request):
        self._require_access(request)
        self._require_json(request)
        if not await self.reconciliation.reconcile():
            return self._error(ConfigUnavailable())
        return await self.handle_get(request)

    async def handle_migration(self, request):
        self._require_access(request)
        self._require_json(request)
        try:
            body = await request.json()
            if not isinstance(body, dict) or type(body.get("apply", False)) is not bool:
                raise ValueError
            result = await self.reconciliation.operation(
                self.editor.store.migrate_cluster, body.get("nodes"),
                base_revision=body.get("base_revision"), apply=body.get("apply", False),
            )
        except (OSError, ValueError, TypeError, KeyError) as error:
            return self._error(error)
        return self._disable_cache(web.json_response(result))

    async def handle_cluster(self, request):
        self._require_access(request)
        return self._disable_cache(web.json_response(self.reconciliation.status()))

    async def handle_health(self, request):
        self._require_access(request)
        healthy = self.reconciliation.http_operational and self.reconciliation.healthy
        return self._disable_cache(web.json_response({"healthy": healthy}, status=200 if healthy else 503))
