"""Symmetric HTTP composition, isolated from app.py and conversation providers."""

import logging

from aiohttp import web

from core.api.control_plane_settings import ControlPlaneSettingsHandler
from core.cluster.config_reconciliation import ConfigReconciliation

LOGGER = logging.getLogger("xiaozhi.control_plane")
RECONCILIATION_KEY = web.AppKey("config_reconciliation", ConfigReconciliation)


@web.middleware
async def safe_errors(request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception:
        # No raw backend exceptions, request bodies or credentials in diagnostics.
        LOGGER.warning("Control-plane HTTP operation unavailable")
        return web.json_response({"error": "Control-plane operation unavailable", "code": "unavailable"}, status=503)


def create_app(store, config, *, client_factory=None):
    if store.bootstrap["config_provider"] != "google_drive":
        raise ValueError("Standalone control plane requires a provisioned Google Drive bootstrap")
    options = {"client_factory": client_factory} if client_factory is not None else {}
    reconciliation = ConfigReconciliation(store, config, **options)
    handler = ControlPlaneSettingsHandler(reconciliation)
    app = web.Application(client_max_size=256 * 1024, middlewares=[safe_errors])
    app[RECONCILIATION_KEY] = reconciliation
    app.add_routes([
        web.get("/settings", handler.handle_redirect), web.get("/settings/", handler.handle_index),
        web.get("/settings/{filename}", handler.handle_asset),
        web.get("/api/settings", handler.handle_get), web.put("/api/settings", handler.handle_put),
        web.get("/api/settings/capabilities", handler.handle_capabilities),
        web.post("/api/settings/sync", handler.handle_sync),
        web.post("/api/settings/migrate-cluster", handler.handle_migration),
        web.get("/api/cluster", handler.handle_cluster), web.get("/healthz", handler.handle_health),
    ])

    async def startup(app):
        await reconciliation.start()
        reconciliation.http_operational = True

    async def shutdown(app):
        reconciliation.http_operational = False

    async def cleanup(app):
        await reconciliation.stop()

    app.on_startup.append(startup)
    app.on_shutdown.append(shutdown)
    app.on_cleanup.append(cleanup)
    return app
