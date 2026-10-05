"""Standalone worker entrypoint. The normal server continues to use app.py."""

import asyncio
import logging
import signal

from core.cluster.nats_config import NatsConfig, WorkerConfigError

LOGGER = logging.getLogger("xiaozhi.worker")


async def run(config: NatsConfig):
    from core.cluster.worker import Worker

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    try:
        await Worker(config, stop).run()
    finally:
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Use credential-safe callbacks instead of dependency exception tracebacks.
    nats_logger = logging.getLogger("nats")
    nats_logger.addHandler(logging.NullHandler())
    nats_logger.propagate = False
    try:
        config = NatsConfig.from_env()
    except WorkerConfigError as error:
        LOGGER.error("Worker configuration invalid: %s", error)
        return 1
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        return 0
    except Exception:
        LOGGER.error("Worker stopped with a runtime error; check dependency and NATS access")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
