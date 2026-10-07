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


def create_app(store, config, *, client_factory=None, secret_exchange=None):
    if store.bootstrap["config_provider"] != "google_drive":
        raise ValueError("Standalone control plane requires a provisioned Google Drive bootstrap")
    options = {"client_factory": client_factory} if client_factory is not None else {}
    reconciliation = ConfigReconciliation(store, config, **options)
    secrets = None
    if config.secrets is not None:
        if config.secrets.address(store.bootstrap["node_id"]) != config.host:
            raise ValueError("Secret peer identity must match the local control-plane listener")
        from core.cluster.secret_provisioning import SecretProvisioning
        secrets = SecretProvisioning(reconciliation, config.secrets, exchange=secret_exchange)
    handler = ControlPlaneSettingsHandler(reconciliation, secrets)
    app = web.Application(client_max_size=256 * 1024, middlewares=[safe_errors])
    app[RECONCILIATION_KEY] = reconciliation
    app.add_routes([
        web.get("/settings", handler.handle_redirect), web.get("/settings/", handler.handle_index),
        web.get("/settings/{filename}", handler.handle_asset),
        web.get("/api/settings", handler.handle_get), web.put("/api/settings", handler.handle_put),
        web.get("/api/settings/capabilities", handler.handle_capabilities),
        web.get("/api/settings/secrets", handler.handle_secret_status),
        web.post("/api/settings/secrets", handler.handle_secret_put),
        web.post("/internal/settings/secret", handler.handle_secret_peer),
        web.post("/api/settings/sync", handler.handle_sync),
        web.post("/api/settings/migrate-cluster", handler.handle_migration),
        web.get("/api/cluster", handler.handle_cluster), web.get("/healthz", handler.handle_health),
        web.get("/api/cluster/soundbank", handler.handle_soundbank_cluster),
        web.get("/api/cluster/diagnostics", handler.handle_voice_diagnostics),
    ])
    if config.bootstrap is not None:
        from core.cluster.mqtt_bootstrap import MqttBootstrap
        bootstrap = MqttBootstrap(reconciliation, config.bootstrap)
        app.router.add_get("/xiaozhi/ota/", bootstrap.handle)
        app.router.add_post("/xiaozhi/ota/", bootstrap.handle)

    async def startup(app):
        await reconciliation.start()
        reconciliation.http_operational = True

    async def shutdown(app):
        reconciliation.http_operational = False

    async def cleanup(app):
        if secrets is not None:
            await secrets.stop()
        await reconciliation.stop()

    app.on_startup.append(startup)
    app.on_shutdown.append(shutdown)
    app.on_cleanup.append(cleanup)
    return app
