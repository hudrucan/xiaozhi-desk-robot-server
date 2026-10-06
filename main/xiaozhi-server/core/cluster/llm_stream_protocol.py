"""Ordered, acknowledged Core NATS text stream; no provider configuration."""
import re

from . import llm_protocol as rpc
from .nats_config import validate_worker_id

PROTOCOL = 'xiaozhi-llm-stream-v1'
SUBJECT = 'xiaozhi.v1.llm.stream'
QUEUE_GROUP = rpc.QUEUE_GROUP
MAX_CHUNK_BYTES = 4096
MAX_EVENT_BYTES = 8192
MAX_CHUNKS = 4096
ACK_SECONDS = 2
ERRORS = rpc.ERRORS | {'llm_stream_unavailable'}


def request(data):
    value = rpc.decode(data, rpc.MAX_REQUEST_BYTES)
    if not isinstance(value, dict) or value.get('protocol') != PROTOCOL:
        raise ValueError('Invalid stream request')
    # Reuse all dialogue, identity, deadline and revision validation without
    # extending or weakening the original final-response request schema.
    legacy = {**value, 'protocol': rpc.PROTOCOL}
    rpc.request(rpc.encode(legacy, rpc.MAX_REQUEST_BYTES))
    return value


def event(value, worker_id, kind, seq, **fields):
    payload = {'protocol': PROTOCOL, 'request_id': value['request_id'],
               'worker_id': worker_id, 'revision': value['revision'],
               'kind': kind, 'seq': seq, **fields}
    data = rpc.encode(payload, MAX_EVENT_BYTES)
    parse_event(data, value['request_id'], value['revision'])
    return data


def parse_event(data, request_id, revision):
    value = rpc.decode(data, MAX_EVENT_BYTES)
    if (not isinstance(value, dict) or value.get('protocol') != PROTOCOL
            or value.get('request_id') != request_id
            or type(value.get('revision')) is not int or value['revision'] != revision
            or not isinstance(value.get('worker_id'), str)
            or type(value.get('seq')) is not int or not 0 <= value['seq'] <= MAX_CHUNKS + 1):
        raise ValueError('Invalid stream correlation')
    validate_worker_id(value['worker_id'])
    common = {'protocol', 'request_id', 'worker_id', 'revision', 'kind', 'seq'}
    kind = value.get('kind')
    if kind == 'started':
        valid = set(value) == common and value['seq'] == 0
    elif kind == 'chunk':
        valid = (set(value) == common | {'text'} and 1 <= value['seq'] <= MAX_CHUNKS
                 and isinstance(value['text'], str)
                 and 0 < len(value['text'].encode('utf-8')) <= MAX_CHUNK_BYTES)
    elif kind == 'complete':
        valid = (set(value) == common | {'text_bytes', 'sha256'}
                 and type(value['text_bytes']) is int and 0 < value['text_bytes'] <= rpc.MAX_TEXT_BYTES
                 and isinstance(value['sha256'], str)
                 and re.fullmatch('[0-9a-f]{64}', value['sha256']) is not None)
    elif kind == 'error':
        valid = set(value) == common | {'error'} and value['error'] in ERRORS
    else:
        valid = False
    if not valid:
        raise ValueError('Invalid stream event')
    return value


def ack(event_value):
    return rpc.encode({key: event_value[key] for key in
        ('protocol', 'request_id', 'worker_id', 'revision', 'seq')}, 1024)


def validate_ack(data, event_value):
    value = rpc.decode(data, 1024)
    if (not isinstance(value, dict) or type(value.get('seq')) is not int
            or type(value.get('revision')) is not int
            or value != rpc.decode(ack(event_value), 1024)):
        raise ValueError('Invalid stream acknowledgment')


def chunks(text):
    # A UTF-8 scalar is at most four bytes. Preserve every character exactly,
    # even when a provider emits an unusually large single chunk.
    for offset in range(0, len(text), MAX_CHUNK_BYTES // 4):
        yield text[offset:offset + MAX_CHUNK_BYTES // 4]
