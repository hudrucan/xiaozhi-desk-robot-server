"""Explicit user-owned synthesis benchmark; no audio playback or config writes."""
import argparse
import asyncio
import json
import logging
import time
from pathlib import Path

from core.cluster.nats_config import NatsConnectionConfig, validate_worker_id
from core.cluster.tts_config import load_bundle
from core.cluster.tts_client import TTSPool
from core.cluster.worker_rpc import WorkerRPC


async def probe(args):
    node = validate_worker_id(args.node_id)
    bundle = load_bundle(args.bundle, node)
    raw = Path(args.segments_file).read_bytes()
    if len(raw) > 32768:
        raise ValueError('Oversized benchmark input')
    segments = json.loads(raw)
    if not isinstance(segments, list) or not 1 <= len(segments) <= 12:
        raise ValueError('Supply one to twelve explicit text segments')
    rpc = WorkerRPC(NatsConnectionConfig.from_env(), node)
    tasks = []
    try:
        await asyncio.wait_for(rpc._connect_once(), 8)
        pool, slots = TTSPool(rpc, bundle), asyncio.Semaphore(args.concurrency)
        async def generate(index, text):
            async with slots:
                return await pool.generate(index, text)
        started = time.monotonic()
        tasks = [asyncio.create_task(generate(index, text)) for index, text in enumerate(segments)]
        results = await asyncio.gather(*tasks)
        elapsed = time.monotonic() - started
        durations = [len(result.pcm) / (2 * result.sample_rate) for result in results]
        print(json.dumps({'protocol': 'xiaozhi-tts-segment-benchmark-v1',
            'concurrency': args.concurrency, 'wall_ms': round(elapsed * 1000),
            'audio_seconds': round(sum(durations), 3),
            'effective_synthesis_rtf': round(elapsed / sum(durations), 3),
            'segments': [{'index': r.index, 'worker_id': r.worker_id, 'synth_ms': r.synth_ms,
                'audio_ms': round(duration * 1000), 'native_rtf': round(r.synth_ms / (duration * 1000), 3)}
                for r, duration in zip(results, durations)]}))
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await rpc.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node-id', required=True)
    parser.add_argument('--bundle', required=True)
    parser.add_argument('--segments-file', required=True)
    parser.add_argument('--concurrency', choices=(1, 3), type=int, required=True)
    args = parser.parse_args()
    # SDK/transport errors may contain credentials; print fixed failure only.
    logging.basicConfig(level=logging.CRITICAL)
    logger = logging.getLogger('nats')
    logger.addHandler(logging.NullHandler()); logger.propagate = False
    try:
        asyncio.run(probe(args))
        return 0
    except Exception:
        print('TTS segment benchmark unavailable; check bundles, workers and private NATS environment')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
