"""Explicit combined ASR/LLM worker with immutable node-local bundles."""
import asyncio
import logging
import os
import signal

from core.cluster.asr_config import load_bundle as load_asr
from core.cluster.llm_config import load_bundle as load_llm
from core.cluster.nats_config import NatsConfig


async def run(config, asr, llm):
    from functools import partial
    from core.cluster.activity import Activity
    from core.cluster.asr_pipeline import ASRPipeline, SileroEngine
    from core.cluster.asr_worker import ASRService
    from core.cluster.llm_config import create_provider
    from core.cluster.llm_worker import LLMWorker
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    # No provider/network connection is opened by ASR model initialization.
    engine = await asyncio.to_thread(SileroEngine, asr['vad'])
    provider = create_provider(llm)
    stream_factory = None
    if asr['provider']['type'] == 'sherpa_streaming':
        from core.providers.asr.sherpa_stream import SherpaEngine, SherpaStream
        asr_engine = await asyncio.to_thread(SherpaEngine, asr['provider'])
        stream_factory = partial(SherpaStream, engine=asr_engine)
    activity = Activity(os.environ.get('XIAOZHI_WORKER_ACTIVITY_PATH', ''), config.worker_id)

    class Combined(LLMWorker):
        async def _start(self):
            await super()._start()
            self.asr = ASRService(self.client, config.worker_id, asr,
                partial(ASRPipeline, engine=engine, stream_factory=stream_factory), activity)
            await self.asr.start()

        async def _disconnected(self):
            if hasattr(self, 'asr'):
                await self.asr.stop()
            await super()._disconnected()

        async def _reconnected(self):
            if hasattr(self, 'asr'):
                self.asr.stopping = False
            await super()._reconnected()

        async def _shutdown(self):
            if hasattr(self, 'asr'):
                await self.asr.stop()
            await super()._shutdown()

    worker = Combined(config, stop, llm, provider)
    worker.activity = activity
    reporter = asyncio.create_task(activity.run())
    try:
        await worker.run()
    finally:
        reporter.cancel()
        await asyncio.gather(reporter, return_exceptions=True)
        await asyncio.wait_for(provider.client.aio.aclose(), timeout=5)
        provider.client.close()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    for name in ('nats', 'google', 'httpx', 'httpcore'):
        logger = logging.getLogger(name)
        logger.addHandler(logging.NullHandler()); logger.propagate = False
    try:
        config = NatsConfig.from_env()
        asr = load_asr(os.environ.get('XIAOZHI_WORKER_ASR_CONFIG', ''), config.worker_id)
        llm = load_llm(os.environ.get('XIAOZHI_WORKER_LLM_CONFIG', ''), config.worker_id)
        if asr['revision'] != llm['revision']:
            raise ValueError('Worker ASR/LLM revisions differ')
        asyncio.run(run(config, asr, llm))
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        logging.error('ASR worker unavailable; validate private bundles, dependencies and local VAD model')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
