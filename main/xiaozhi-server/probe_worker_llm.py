"""Authenticated private core -> NATS -> LLM probe; never prints credentials."""
import argparse
import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import time

from aiohttp import ClientSession, ClientTimeout
from core.cluster import llm_protocol as wire


def headers(key, now=None):
    if not isinstance(key, str) or not key.strip() or len(key) > 4096 or any(c in key for c in '\x00\r\n'):
        raise ValueError('Invalid probe authentication')
    stamp = str(int(time.time() if now is None else now))
    identity, mac = 'xiaozhi-llm-probe', '02:00:00:00:00:01'
    digest = hmac.new(key.encode(), f'{identity}|{mac}|{stamp}'.encode(), hashlib.sha256).digest()
    token = base64.urlsafe_b64encode(digest).decode().rstrip('=')
    return {'device-id': mac, 'client-id': identity, 'authorization': f'Bearer {token}.{stamp}'}


def request_body(revision, prompt, seconds):
    if type(revision) is not int or revision < 1 or type(seconds) is not int or not 1 <= seconds <= wire.MAX_SECONDS:
        raise ValueError('Invalid probe revision or deadline')
    messages = wire.dialogue([{'role': 'user', 'content': prompt}])
    return wire.encode({'revision': revision, 'dialogue': messages, 'timeout_seconds': seconds}, 8192)


def result_summary(data, revision, elapsed, expected=None, show_text=False):
    value = wire.decode(data, wire.MAX_REPLY_BYTES)
    if not isinstance(value, dict) or not isinstance(value.get('request_id'), str):
        raise ValueError('Invalid probe result')
    wire.identity(value['request_id'])
    value = wire.reply(data, value['request_id'], revision)
    result = {'status': value['status'], 'worker_id': value['worker_id'],
              'revision': revision, 'elapsed_seconds': round(elapsed, 3)}
    if value['status'] != 'ok':
        result['error'] = value['error']
        return result
    result['text_chars'] = len(value['text'])
    if expected is not None:
        result['expected_text_found'] = expected in value['text']
    if show_text:
        result['text'] = value['text']
    return result


async def read_bounded(stream, limit):
    parts, size = [], 0
    while True:
        part = await stream.read(min(8192, limit + 1 - size))
        if not part:
            return b''.join(parts)
        size += len(part)
        if size > limit:
            raise ValueError('Oversized core reply')
        parts.append(part)


async def probe(core_ip, port, revision, prompt, seconds, key, *, expected=None, show_text=False, cancel_after=None):
    if str(ipaddress.IPv4Address(core_ip)) not in {'10.10.10.11', '10.10.10.12', '10.10.10.13'}:
        raise ValueError('Probe requires a private Desk core address')
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('Invalid core port')
    payload = request_body(revision, prompt, seconds)
    if cancel_after is not None and not 0.1 <= cancel_after < min(seconds, 6):
        raise ValueError('Invalid probe cancellation interval')
    started = time.monotonic()
    async with ClientSession(trust_env=False, timeout=ClientTimeout(total=seconds + 5, connect=3)) as client:
        pending = asyncio.create_task(client.post(f'http://{core_ip}:{port}/api/workers/llm', data=payload,
            headers={**headers(key), 'Content-Type': 'application/json'}, allow_redirects=False))
        if cancel_after is not None:
            done, _ = await asyncio.wait((pending,), timeout=cancel_after)
            if not done:
                pending.cancel()
                await client.close()
                await asyncio.gather(pending, return_exceptions=True)
                return {'status': 'cancelled', 'client_request_closed': True,
                        'elapsed_seconds': round(time.monotonic() - started, 3)}
        async with await pending as response:
            data = await read_bounded(response.content, wire.MAX_REPLY_BYTES)
            if response.status not in (200, 503):
                return {'status': 'error', 'error': 'core_http_rejected', 'http_status': response.status}
            if response.status == 503:
                value = wire.decode(data, wire.MAX_REPLY_BYTES)
                if isinstance(value, dict) and set(value) == {'error'} and value['error'] in {
                        'worker_rpc_unavailable', 'worker_rpc_busy', 'worker_rpc_invalid_reply', 'worker_rpc_failed'}:
                    return {'status': 'error', 'error': value['error'], 'http_status': 503}
            result = result_summary(data, revision, time.monotonic() - started, expected, show_text)
            if (response.status == 200) != (result['status'] == 'ok'):
                raise ValueError('Inconsistent core result')
            return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--core-ip', required=True)
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--revision', type=int, required=True)
    parser.add_argument('--prompt', default='Reply with exactly: LLM OK')
    parser.add_argument('--timeout-seconds', type=int, default=30)
    parser.add_argument('--expect')
    parser.add_argument('--show-text', action='store_true')
    parser.add_argument('--cancel-after', type=float, help='Close the HTTP request after this many seconds; verify worker cleanup separately')
    args = parser.parse_args()
    try:
        result = asyncio.run(probe(args.core_ip, args.port, args.revision, args.prompt, args.timeout_seconds,
            os.environ.get('XIAOZHI_CORE_AUTH_KEY', ''), expected=args.expect, show_text=args.show_text, cancel_after=args.cancel_after))
    except Exception:
        result = {'status': 'error', 'error': 'llm_probe_unavailable'}
    print(json.dumps(result, ensure_ascii=False))
    passed = (result['status'] == 'ok' and result.get('expected_text_found', True)) or (
        args.cancel_after is not None and result['status'] == 'cancelled')
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
