"""Versioned, bounded Core NATS streaming ASR envelopes."""
import re
import time

from .llm_protocol import decode, encode, identity
from .nats_config import validate_worker_id

PROTOCOL = 'xiaozhi-asr-stream-v1'
OPEN_SUBJECT = 'xiaozhi.v1.asr.open'
QUEUE_GROUP = 'xiaozhi-asr-workers'
MAX_AUDIO = 4096
MAX_TEXT = 16384
MAX_SECONDS = 40
LEASE_SECONDS = 4
ERRORS = {'asr_busy', 'asr_expired', 'asr_revision_mismatch', 'asr_gap',
          'asr_overflow', 'asr_provider_failed', 'asr_unavailable', 'asr_invalid_audio'}


def input_subject(worker_id):
    return 'xiaozhi.v1.asr.' + validate_worker_id(worker_id) + '.input'


def open_request(data):
    value = decode(data, 2048)
    if (not isinstance(value, dict) or set(value) != {'protocol', 'turn_id', 'core_id',
            'revision', 'deadline_ms', 'result_subject', 'mode', 'audio'}
            or value['protocol'] != PROTOCOL or type(value['revision']) is not int
            or value['revision'] < 1 or type(value['deadline_ms']) is not int
            or value['deadline_ms'] > int(time.time() * 1000) + (MAX_SECONDS + 1) * 1000
            or value['mode'] not in ('manual', 'auto')
            or not isinstance(value['result_subject'], str)
            or not re.fullmatch(r'_INBOX\.[A-Za-z0-9_-]{16,128}', value['result_subject'])
            or value['audio'] != {'format': 'opus', 'sample_rate': 16000, 'channels': 1, 'frame_duration': 60}
            or any(type(value['audio'][key]) is not int for key in ('sample_rate','channels'))
            or type(value['audio']['frame_duration']) not in (int,float)):
        raise ValueError('Invalid ASR admission')
    identity(value['turn_id'])
    validate_worker_id(value['core_id'])
    return value


def packet(turn_id, token, kind, seq=0, audio=b''):
    meta = {'protocol': PROTOCOL, 'turn_id': turn_id, 'token': token, 'kind': kind, 'seq': seq}
    header = encode(meta, 512)
    data = len(header).to_bytes(2, 'big') + header + audio
    unpack(data)
    return data


def unpack(data):
    if not isinstance(data, bytes) or not 2 < len(data) <= MAX_AUDIO + 514:
        raise ValueError('Invalid ASR packet')
    size = int.from_bytes(data[:2], 'big')
    if not 0 < size <= 512 or len(data) < size + 2:
        raise ValueError('Invalid ASR header')
    value, audio = decode(data[2:2 + size], 512), data[2 + size:]
    if (not isinstance(value, dict) or set(value) != {'protocol', 'turn_id', 'token', 'kind', 'seq'}
            or value['protocol'] != PROTOCOL or value['kind'] not in ('audio', 'end', 'cancel', 'lease')
            or type(value['seq']) is not int or not 0 <= value['seq'] <= 2000
            or (value['kind'] == 'audio' and not 0 < len(audio) <= MAX_AUDIO)
            or (value['kind'] != 'audio' and audio)):
        raise ValueError('Invalid ASR input')
    identity(value['turn_id']); identity(value['token'])
    return value, audio


def event(turn_id, worker_id, revision, token, kind, **fields):
    return encode({'protocol': PROTOCOL, 'turn_id': turn_id,
                   'worker_id': worker_id, 'revision': revision, 'token': token,
                   'kind': kind, **fields}, MAX_TEXT + 1024)


def result(data, turn_id, revision, worker_id=None, token=None):
    value = decode(data, MAX_TEXT + 1024)
    if (not isinstance(value, dict) or not {'protocol', 'turn_id', 'worker_id', 'revision', 'token', 'kind'} <= set(value)
            or value['protocol'] != PROTOCOL or value['turn_id'] != turn_id
            or type(value['revision']) is not int or value['revision'] != revision
            or value['kind'] not in ('admitted', 'ack', 'partial', 'final', 'error')):
        raise ValueError('Invalid ASR reply')
    validate_worker_id(value['worker_id']); identity(value['token'])
    if worker_id is not None and value['worker_id'] != worker_id:
        raise ValueError('ASR worker changed during turn')
    if token is not None and value['token'] != token:
        raise ValueError('Invalid ASR ownership')
    extra = set(value) - {'protocol', 'turn_id', 'worker_id', 'revision', 'token', 'kind'}
    if value['kind'] == 'error':
        if extra != {'error'} or value['error'] not in ERRORS:
            raise ValueError('Invalid ASR error')
    elif value['kind'] in ('partial', 'final'):
        if extra != {'text'} or not isinstance(value['text'], str) or len(value['text'].encode('utf-8')) > MAX_TEXT:
            raise ValueError('Invalid ASR text')
    elif value['kind'] == 'ack':
        if extra != {'seq'} or type(value['seq']) is not int or not 0 <= value['seq'] <= 2000:
            raise ValueError('Invalid ASR acknowledgment')
    elif extra:
        raise ValueError('Invalid ASR admission reply')
    return value
