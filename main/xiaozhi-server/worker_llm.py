"""Opt-in standalone text LLM worker; the original worker.py stays ping-only."""
import asyncio
import logging
import os
import signal

from core.cluster.nats_config import NatsConfig
from core.cluster.llm_config import create_provider, load_bundle

LOGGER = logging.getLogger('xiaozhi.worker.llm')


async def run(config, bundle):
    from core.cluster.llm_worker import LLMWorker
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    provider = create_provider(bundle)
    try:
        await LLMWorker(config, stop, bundle, provider).run()
    finally:
        await asyncio.wait_for(provider.client.aio.aclose(), timeout=5)
        provider.client.close()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    # SDK diagnostics can include URLs or request/response content. Application
    # diagnostics are fixed codes; no provider text or credentials are logged.
    for name in ('nats', 'google', 'httpx', 'httpcore'):
        logger = logging.getLogger(name)
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
    try:
        config = NatsConfig.from_env()
        bundle = load_bundle(os.environ.get('XIAOZHI_WORKER_LLM_CONFIG', ''), config.worker_id)
        asyncio.run(run(config, bundle))
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        LOGGER.error('Text worker unavailable; check local config, credentials and dependencies')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
