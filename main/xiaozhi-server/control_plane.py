"""Dedicated Settings/control-plane process; does not start the conversation server."""

import asyncio
import logging
import signal

from config.control_plane import ControlPlaneConfig

LOGGER = logging.getLogger("xiaozhi.control_plane")


async def run(config):
    from aiohttp import web
    from nats.aio.client import Client
    from config.bootstrap import load_bootstrap
    from config.google_drive_config import GoogleDriveConfigStore
    from core.control_plane import create_app

    bootstrap = load_bootstrap()
    store = GoogleDriveConfigStore(bootstrap)
    app = create_app(store, config, client_factory=Client)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    runner = web.AppRunner(app, access_log=None)
    try:
        await runner.setup()
        await web.TCPSite(runner, config.host, config.port).start()
        LOGGER.info("Control-plane HTTP operational; conversation runtime is not started")
        await stop.wait()
    finally:
        await runner.cleanup()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    nats_logger = logging.getLogger("nats")
    nats_logger.addHandler(logging.NullHandler())
    nats_logger.propagate = False
    try:
        config = ControlPlaneConfig.from_env()
    except ValueError:
        LOGGER.error("Invalid control-plane/NATS environment configuration")
        return 1
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        return 0
    except Exception:
        LOGGER.error("Control plane stopped; check dependencies, local bootstrap and Cloud access")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
