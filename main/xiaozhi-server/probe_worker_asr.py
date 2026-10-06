"""Explicit real ASR probe with a user-provided 16kHz mono PCM WAV."""
import argparse
import asyncio
import json
import logging
import time
import wave


async def probe(args):
    import opuslib_next
    from core.cluster.asr_client import ASRClient
    from core.cluster.nats_config import NatsConnectionConfig, validate_worker_id
    from core.cluster.worker_rpc import WorkerRPC
    validate_worker_id(args.core_id)
    if args.revision < 1:
        raise ValueError('Invalid revision')
    with wave.open(args.wav, 'rb') as source:
        if (source.getframerate() != 16000 or source.getnchannels() != 1 or source.getsampwidth() != 2
                or source.getcomptype() != 'NONE' or not 0 < source.getnframes() <= 480000):
            raise ValueError('Probe requires 16kHz mono PCM16 WAV, maximum 30 seconds')
        pcm = source.readframes(source.getnframes())
    rpc = WorkerRPC(NatsConnectionConfig.from_env(), args.core_id)
    try:
        await asyncio.wait_for(rpc._connect_once(), timeout=8)
        async def partial(text): pass
        audio = {'format':'opus','sample_rate':16000,'channels':1,'frame_duration':60}
        client = ASRClient(rpc.client,args.core_id,args.revision,audio,'manual',partial)
        encoder, queue = opuslib_next.Encoder(16000,1,opuslib_next.APPLICATION_AUDIO), asyncio.Queue(64)
        async def producer():
            for start in range(0,len(pcm),1920):
                frame = pcm[start:start+1920].ljust(1920,b'\0')
                await queue.put(('audio',encoder.encode(frame,960)))
                await asyncio.sleep(0.06)
            await queue.put(('end',b''))
        task = asyncio.create_task(producer())
        started = time.monotonic()
        try:
            text = await client.transcribe(queue)
        finally:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        result = {'status':'ok','worker_id':client.worker_id,'revision':args.revision,
                  'elapsed_seconds':round(time.monotonic()-started,3),'text_chars':len(text)}
        if args.expect:
            result['expected_text_found'] = args.expect.casefold() in text.casefold()
        if args.show_text:
            result['text'] = text
        print(json.dumps(result,ensure_ascii=False))
        return 0 if text and (not args.expect or result['expected_text_found']) else 1
    finally:
        await rpc.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wav',required=True)
    parser.add_argument('--revision',required=True,type=int)
    parser.add_argument('--core-id',default='asr-probe')
    parser.add_argument('--expect')
    parser.add_argument('--show-text',action='store_true')
    args = parser.parse_args()
    for name in ('nats','google','httpx','httpcore'):
        logger=logging.getLogger(name);logger.addHandler(logging.NullHandler());logger.propagate=False
    try:
        return asyncio.run(probe(args))
    except Exception:
        print(json.dumps({'status':'error','error':'asr_probe_unavailable'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
