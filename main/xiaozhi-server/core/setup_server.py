"""Isolated loopback HTTP setup; imports no normal HTTP/runtime handlers."""

import asyncio
import os
import sys
from pathlib import Path

from aiohttp import web

from config.config_loader import get_project_dir, load_default_config
from core.api.setup_handler import SetupHandler, setup_access


def create_setup_app(request_restart, *, data_dir=None, handler=None):
    handler = handler or SetupHandler(request_restart, data_dir=data_dir)
    app = web.Application(client_max_size=40 * 1024, middlewares=[setup_access])
    app.add_routes([
        web.get("/", handler.handle_redirect),
        web.get("/setup", handler.handle_redirect),
        web.get("/setup/", handler.handle_index),
        web.get("/setup/{filename}", handler.handle_asset),
        web.get("/api/setup/status", handler.handle_status),
        web.post("/api/setup/oauth-client", handler.handle_client),
        web.post("/api/setup/oauth/start", handler.handle_oauth_start),
        web.get("/api/setup/oauth/status", handler.handle_oauth_status),
        web.get("/api/setup/sources", handler.handle_sources),
        web.post("/api/setup/restore", handler.handle_restore),
        web.post("/api/setup/restart", handler.handle_restart),
    ])

    async def close(application):
        await handler.close()

    app.on_cleanup.append(close)
    return app


class SetupHttpServer:
    def __init__(self, request_restart, *, port=8003):
        self.port = port
        self.ready = asyncio.Event()
        self.app = create_setup_app(request_restart)

    async def start(self):
        runner = web.AppRunner(self.app, access_log=None)
        try:
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", self.port).start()
            self.ready.set()
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()


async def run_setup(wait_for_exit, wait_for_listener):
    # Read reference HTTP settings only; never load/prepare the runtime store.
    defaults = load_default_config(Path(get_project_dir()) / "config.yaml")
    port = int(defaults.get("server", {}).get("http_port", 8003))
    if not 1 <= port <= 65535:
        raise ValueError("Setup requires a valid default HTTP port")
    print(f"SETUP MODE: http://localhost:{port}/setup/")
    print("For a headless server, run on the laptop (replace user@host):")
    print(f"ssh -N -o ExitOnForwardFailure=yes -L {port}:127.0.0.1:{port} -L 8765:127.0.0.1:8765 user@host")
    restart = asyncio.Event()
    server = SetupHttpServer(restart.set, port=port)
    task = asyncio.create_task(server.start())
    should_restart = False
    try:
        await wait_for_listener(server, task)
        should_restart = await wait_for_exit(restart)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    if should_restart:
        os.execv(sys.executable, [sys.executable, *sys.argv])
