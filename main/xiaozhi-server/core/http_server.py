import asyncio
from aiohttp import web
from config.logger import setup_logging
from core.api.memory_handler import MemoryHandler
from core.api.ota_handler import OTAHandler
from core.api.settings_handler import SettingsHandler
from core.api.vision_handler import VisionHandler

TAG = __name__


class SimpleHttpServer:
    def __init__(self, config: dict, request_restart=None, memory_provider=None):
        self.config = config
        self.logger = setup_logging()
        self.ota_handler = OTAHandler(config)
        self.vision_handler = VisionHandler(config)
        self.settings_handler = None
        self.memory_handler = None
        settings_config = config.get("server", {}).get("settings", {})
        if settings_config.get("enabled", True) and request_restart is not None:
            self.settings_handler = SettingsHandler(config, request_restart)
            self.memory_handler = MemoryHandler(config, memory_provider)

    def _get_websocket_url(self, local_ip: str, port: int) -> str:
        """Return the WebSocket URL advertised by the bootstrap endpoint.

        Args:
            local_ip: Local server IP address.
            port: WebSocket listener port.

        Returns:
            str: WebSocket URL.
        """
        server_config = self.config["server"]
        websocket_config = server_config.get("websocket")

        if websocket_config and "你" not in websocket_config:
            return websocket_config
        else:
            return f"ws://{local_ip}:{port}/xiaozhi/v1/"

    async def start(self):
        runner = None
        try:
            server_config = self.config["server"]
            host = server_config.get("ip", "0.0.0.0")
            port = int(server_config.get("http_port", 8003))

            if port:
                app = web.Application()

                app.add_routes(
                    [
                        web.get("/xiaozhi/ota/", self.ota_handler.handle_get),
                        web.post("/xiaozhi/ota/", self.ota_handler.handle_post),
                        web.options("/xiaozhi/ota/", self.ota_handler.handle_options),
                        # Downloads are restricted to data/bin/*.bin.
                        web.get(
                            "/xiaozhi/ota/download/{filename}",
                            self.ota_handler.handle_download,
                        ),
                        web.options(
                            "/xiaozhi/ota/download/{filename}",
                            self.ota_handler.handle_options,
                        ),
                    ]
                )

                if self.settings_handler is not None:
                    app.add_routes(
                        [
                            web.get("/settings", self.settings_handler.handle_redirect),
                            web.get("/settings/", self.settings_handler.handle_index),
                            web.get(
                                "/settings/{filename}",
                                self.settings_handler.handle_asset,
                            ),
                            web.get(
                                "/api/settings", self.settings_handler.handle_get
                            ),
                            web.get(
                                "/api/settings/status",
                                self.settings_handler.handle_status,
                            ),
                            web.put(
                                "/api/settings", self.settings_handler.handle_put
                            ),
                            web.post(
                                "/api/settings/restart",
                                self.settings_handler.handle_restart,
                            ),
                            web.get(
                                "/api/settings/memory",
                                self.memory_handler.handle_get,
                            ),
                            web.post(
                                "/api/settings/memory",
                                self.memory_handler.handle_post,
                            ),
                            web.put(
                                "/api/settings/memory/{entry_id}",
                                self.memory_handler.handle_put,
                            ),
                            web.delete(
                                "/api/settings/memory/{entry_id}",
                                self.memory_handler.handle_delete,
                            ),
                        ]
                    )
                # Vision routes.
                app.add_routes(
                    [
                        web.get("/mcp/vision/explain", self.vision_handler.handle_get),
                        web.post(
                            "/mcp/vision/explain", self.vision_handler.handle_post
                        ),
                        web.options(
                            "/mcp/vision/explain", self.vision_handler.handle_options
                        ),
                    ]
                )

                # Start serving HTTP requests.
                runner = web.AppRunner(app)
                await runner.setup()
                site = web.TCPSite(runner, host, port)
                await site.start()

                # Keep the service alive until the task is cancelled.
                while True:
                    await asyncio.sleep(3600)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Failed to start HTTP server: {e}")
            import traceback

            self.logger.bind(tag=TAG).error(f"Stack trace: {traceback.format_exc()}")
            raise
        finally:
            self.vision_handler.close()
            if runner is not None:
                await runner.cleanup()
