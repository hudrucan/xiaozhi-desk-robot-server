"""ASR/LLM worker and optional segment TTS with immutable node-local bundles."""
import asyncio
import logging
import os
import signal

from core.cluster.asr_config import load_bundle as load_asr
from core.cluster.llm_config import load_bundle as load_llm
from core.cluster.nats_config import NatsConfig


async def run(config, asr, llm, tts=None):
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
    tts_engine = None
    if tts is not None:
        from core.providers.tts.sherpa_worker import SherpaSegmentEngine
        tts_engine = await asyncio.to_thread(SherpaSegmentEngine, tts)
        activity.counts['tts'] = 0

    class Combined(LLMWorker):
        async def _start(self):
            await super()._start()
            self.asr = ASRService(self.client, config.worker_id, asr,
                partial(ASRPipeline, engine=engine, stream_factory=stream_factory), activity)
            await self.asr.start()
            if tts is not None:
                from core.cluster.tts_worker import TTSService
                self.tts = TTSService(self.client, config.worker_id, tts, tts_engine, activity)
                await self.tts.start()
            if 'vision' in llm:
                from core.cluster.vision_worker import VisionService
                from core.cluster.vision_config import GeminiVision
                self.vision = VisionService(self.client, config.worker_id, llm['revision'], GeminiVision(llm['vision']), activity)
                await self.vision.start()

        async def _disconnected(self):
            # Invalidate LLM streams before waiting for uninterruptible native
            # inference. Core reconnect must never revive an old text stream.
            await super()._disconnected()
            await asyncio.gather(*(getattr(self, name).stop()
                for name in ('asr', 'tts', 'vision') if hasattr(self, name)))

        async def _reconnected(self):
            if hasattr(self, 'vision'):
                self.vision.stopping = False
            if hasattr(self, 'tts'):
                self.tts.stopping = False
            if hasattr(self, 'asr'):
                self.asr.stopping = False
            await super()._reconnected()

        async def _shutdown(self):
            self.stopping = True
            for _, task in tuple(self.jobs.values()):
                task.cancel()
            await asyncio.gather(*(getattr(self, name).stop()
                for name in ('asr', 'tts', 'vision') if hasattr(self, name)))
            if hasattr(self, 'vision'):
                await self.vision.close()
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
        tts = None
        if 'XIAOZHI_WORKER_TTS_CONFIG' in os.environ:
            from core.cluster.tts_config import load_bundle
            tts = load_bundle(os.environ['XIAOZHI_WORKER_TTS_CONFIG'], config.worker_id)
            if config.worker_id not in tts['workers']:
                raise ValueError('Worker is absent from TTS membership')
            if tts['revision'] != llm['revision']:
                raise ValueError('Worker TTS/ASR/LLM revisions differ')
        asyncio.run(run(config, asr, llm, tts))
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        logging.error('Voice worker unavailable; validate enabled private bundles, dependencies and local models')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
