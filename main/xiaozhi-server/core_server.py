"""Standalone cluster transport core; provider execution is not enabled yet."""

import asyncio
import logging
import signal

from core.cluster.transport_core import CoreConfig, TransportCore


async def main():
    config = CoreConfig.from_env()
    import os
    enabled = os.environ.get("XIAOZHI_CORE_WORKER_RPC", "false")
    if enabled not in ("true", "false"):
        raise ValueError("Invalid worker RPC flag")
    rpc = None
    if enabled == "true":
        from core.cluster.nats_config import NatsConnectionConfig
        from core.cluster.worker_rpc import WorkerRPC
        rpc = WorkerRPC(NatsConnectionConfig.from_env(), config.node_id)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    core = TransportCore(config, rpc)
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
