"""Standalone cluster transport core; provider execution is not enabled yet."""

import asyncio
import logging
import signal

from core.cluster.transport_core import CoreConfig, TransportCore


async def main():
    config = CoreConfig.from_env()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    core = TransportCore(config)
    try:
        await core.start()
        await stop.wait()
    finally:
        await core.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        asyncio.run(main())
    except ValueError:
        logging.error("Invalid core configuration; check XIAOZHI_CORE_* environment values")
        raise SystemExit(1) from None
