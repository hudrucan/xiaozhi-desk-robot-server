"""Targeted one-slot TTS admission and bounded pull of PCM segment results."""
import re
import time

from .llm_protocol import decode, encode, identity
from .nats_config import validate_worker_id

PROTOCOL = 'xiaozhi-tts-segment-v1'
MAX_REQUEST = 4096
MAX_TEXT = 2048
MAX_PCM_CHUNK = 32768
MAX_PCM = 3 * 1024 * 1024
MAX_SECONDS = 45
LEASE_SECONDS = 4
MAX_SEGMENTS = 512
RATES = (16000, 22050, 24000, 44100, 48000)
ERRORS = {'tts_busy', 'tts_expired', 'tts_revision_mismatch', 'tts_voice_mismatch',
    'tts_unavailable', 'tts_provider_failed', 'tts_invalid_result', 'tts_segment_too_large'}


def subject(worker_id):
    return 'xiaozhi.v1.tts.' + validate_worker_id(worker_id) + '.segment'


def request(data):
    value = decode(data, MAX_REQUEST)
    common = {'protocol', 'op', 'core_id', 'revision', 'fingerprint'}
    if (not isinstance(value, dict) or value.get('protocol') != PROTOCOL
            or type(value.get('revision')) is not int or value['revision'] < 1
            or not isinstance(value.get('fingerprint'), str)
            or not re.fullmatch('[0-9a-f]{64}', value['fingerprint'])
            or not isinstance(value.get('core_id'), str)):
        raise ValueError('Invalid TTS request')
    validate_worker_id(value['core_id'])
    if value['op'] == 'status':
        if set(value) != common:
            raise ValueError('Invalid TTS status request')
        return value
    owned = common | {'job_id', 'token', 'index', 'deadline_ms'}
    extra = {'text'} if value['op'] == 'admit' else {'offset'} if value['op'] == 'poll' else set()
    if value['op'] not in ('admit', 'poll', 'cancel', 'done') or set(value) != owned | extra:
        raise ValueError('Invalid TTS operation')
    identity(value['job_id']); identity(value['token'])
    if (type(value['index']) is not int or not 0 <= value['index'] < MAX_SEGMENTS
            or type(value['deadline_ms']) is not int
            or value['deadline_ms'] > int(time.time() * 1000) + (MAX_SECONDS + 1) * 1000):
        raise ValueError('Invalid TTS deadline/index')
    if value['op'] == 'admit' and (not isinstance(value['text'], str)
            or not 0 < len(value['text'].encode('utf-8')) <= MAX_TEXT or len(value['text']) > 512):
        raise ValueError('TTS segment exceeds text bound')
    if value['op'] == 'poll' and (type(value['offset']) is not int
            or not 0 <= value['offset'] <= MAX_PCM or value['offset'] % 2):
        raise ValueError('Invalid PCM offset')
    return value


def pack(header, pcm=b''):
    metadata = encode(header, 2048)
    if len(pcm) > MAX_PCM_CHUNK or len(pcm) % 2:
        raise ValueError('Invalid PCM chunk')
    return len(metadata).to_bytes(2, 'big') + metadata + pcm


def unpack(data):
    if not isinstance(data, bytes) or not 3 <= len(data) <= MAX_PCM_CHUNK + 2050:
        raise ValueError('Invalid TTS result size')
    size = int.from_bytes(data[:2], 'big')
    if not 0 < size <= 2048 or size + 2 > len(data):
        raise ValueError('Invalid TTS result header')
    header, pcm = decode(data[2:2 + size], 2048), data[2 + size:]
    if not isinstance(header, dict) or len(pcm) > MAX_PCM_CHUNK or len(pcm) % 2:
        raise ValueError('Invalid TTS result')
    return header, pcm


def response_header(value, worker_id, state, **fields):
    return {'protocol': PROTOCOL, 'worker_id': worker_id, 'revision': value['revision'],
        'fingerprint': value['fingerprint'], 'job_id': value['job_id'], 'token': value['token'],
        'index': value['index'], 'state': state, **fields}


def parse_response(data, value, worker_id):
    header, pcm = unpack(data)
    common = {'protocol', 'worker_id', 'revision', 'fingerprint', 'job_id', 'token', 'index', 'state'}
    expected = response_header(value, worker_id, header.get('state'))
    if any(type(header.get(key)) is not type(v) or header[key] != v for key, v in expected.items()):
        raise ValueError('Invalid TTS response ownership')
    state = header['state']
    if state in ('admitted', 'pending', 'released'):
        valid = set(header) == common and not pcm
    elif state == 'error':
        valid = set(header) == common | {'error'} and not pcm and header['error'] in ERRORS
    elif state == 'audio':
        valid = (set(header) == common | {'offset', 'total_bytes', 'sample_rate', 'sha256', 'synth_ms'}
            and type(header['offset']) is int and header['offset'] == value['offset']
            and type(header['total_bytes']) is int and 0 < header['total_bytes'] <= MAX_PCM
            and header['total_bytes'] % 2 == 0 and 0 < len(pcm) <= header['total_bytes'] - header['offset']
            and type(header['sample_rate']) is int and header['sample_rate'] in RATES
            and header['total_bytes'] <= header['sample_rate'] * 2 * 30
            and type(header['synth_ms']) is int and header['synth_ms'] >= 0
            and isinstance(header['sha256'], str) and re.fullmatch('[0-9a-f]{64}', header['sha256']) is not None)
    else:
        valid = False
    if not valid:
        raise ValueError('Invalid TTS response contract')
    return header, pcm
