"""Bounded text-only LLM RPC; no provider config or credentials in requests."""
import json
import re
import time

from .nats_config import validate_worker_id

PROTOCOL = 'xiaozhi-llm-rpc-v1'
SUBJECT = 'xiaozhi.v1.llm.generate'
CANCEL_SUBJECT = 'xiaozhi.v1.llm.cancel'
QUEUE_GROUP = 'xiaozhi-llm-workers'
MAX_REQUEST_BYTES = 32768
MAX_TEXT_BYTES = 65536
MAX_REPLY_BYTES = 70000
MAX_SECONDS = 30
ERRORS = {'llm_busy', 'llm_expired', 'llm_cancelled', 'llm_revision_mismatch',
          'llm_output_too_large', 'llm_provider_failed', 'llm_empty_response'}


def encode(value, limit):
    data = json.dumps(value, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')
    if len(data) > limit:
        raise ValueError('RPC payload exceeds limit')
    return data


def decode(data, limit):
    if not isinstance(data, bytes) or not 0 < len(data) <= limit:
        raise ValueError('Invalid RPC size')
    def unique(pairs):
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError('Duplicate RPC fields')
        return value
    return json.loads(data.decode('utf-8'), object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite RPC value')))


def identity(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{32}', value):
        raise ValueError('Invalid RPC identity')
    return value


def dialogue(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise ValueError('Invalid dialogue')
    for message in value:
        if (not isinstance(message, dict) or set(message) != {'role', 'content'}
                or message['role'] not in ('user', 'assistant')
                or not isinstance(message['content'], str)
                or not 0 < len(message['content'].encode('utf-8')) <= 16384):
            raise ValueError('Invalid dialogue message')
    if value[-1]['role'] != 'user':
        raise ValueError('Dialogue must end with a user message')
    encode(value, MAX_REQUEST_BYTES - 1024)
    return value


def request(data, now_ms=None):
    value = decode(data, MAX_REQUEST_BYTES)
    if (not isinstance(value, dict) or set(value) != {'protocol', 'request_id', 'cancel_token', 'core_id', 'revision', 'deadline_ms', 'dialogue'}
            or value['protocol'] != PROTOCOL or type(value['revision']) is not int or value['revision'] < 1
            or type(value['deadline_ms']) is not int):
        raise ValueError('Invalid LLM request')
    identity(value['request_id']); identity(value['cancel_token'])
    if not isinstance(value['core_id'], str):
        raise ValueError('Invalid core identity')
    validate_worker_id(value['core_id'])
    dialogue(value['dialogue'])
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    # Expired requests get a fixed reply; excessive future deadlines are invalid.
    if value['deadline_ms'] > now_ms + (MAX_SECONDS + 1) * 1000:
        raise ValueError('Invalid deadline')
    return value


def cancellation(data):
    value = decode(data, 512)
    if not isinstance(value, dict) or set(value) != {'protocol', 'request_id', 'cancel_token'} or value['protocol'] != PROTOCOL:
        raise ValueError('Invalid cancellation')
    identity(value['request_id']); identity(value['cancel_token'])
    return value


def response(request_value, worker_id, *, text=None, error=None):
    value = {'protocol': PROTOCOL, 'request_id': request_value['request_id'],
             'worker_id': validate_worker_id(worker_id), 'revision': request_value['revision']}
    if error is not None:
        if error not in ERRORS:
            raise ValueError('Invalid public error')
        value.update(status='error', error=error)
    else:
        if not isinstance(text, str) or not 0 < len(text.encode('utf-8')) <= MAX_TEXT_BYTES:
            raise ValueError('Invalid result text')
        value.update(status='ok', text=text)
    return encode(value, MAX_REPLY_BYTES)


def reply(data, request_id, revision):
    value = decode(data, MAX_REPLY_BYTES)
    if (not isinstance(value, dict) or value.get('protocol') != PROTOCOL
            or value.get('request_id') != request_id or type(value.get('revision')) is not int
            or value['revision'] != revision or not isinstance(value.get('worker_id'), str)):
        raise ValueError('Invalid LLM reply correlation')
    validate_worker_id(value['worker_id'])
    common = {'protocol', 'request_id', 'worker_id', 'revision', 'status'}
    if value.get('status') == 'ok':
        if (set(value) != common | {'text'} or not isinstance(value['text'], str)
                or not 0 < len(value['text'].encode('utf-8')) <= MAX_TEXT_BYTES):
            raise ValueError('Invalid LLM result')
    elif value.get('status') == 'error':
        if set(value) != common | {'error'} or value['error'] not in ERRORS:
            raise ValueError('Invalid LLM error')
    else:
        raise ValueError('Invalid LLM status')
    return value
