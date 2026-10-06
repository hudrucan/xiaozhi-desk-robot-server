"""Immutable ASR/VAD deployment bundle, never accepted from NATS."""
import json
import math
import os
import re
import stat
from pathlib import Path

from .nats_config import validate_worker_id


def validate_bundle(value, worker_id):
    validate_worker_id(worker_id)
    if (not isinstance(value, dict) or set(value) != {'protocol', 'worker_id', 'revision', 'provider', 'vad'}
            or value['protocol'] != 'xiaozhi-worker-asr-config-v1' or value['worker_id'] != worker_id
            or type(value['revision']) is not int or value['revision'] < 1):
        raise ValueError('Invalid ASR configuration')
    provider, vad = value['provider'], value['vad']
    if not isinstance(provider, dict) or provider.get('type') not in ('gemini', 'sherpa_streaming'):
        raise ValueError('Unsupported ASR provider')
    if provider['type'] == 'sherpa_streaming':
        if (set(provider) != {'type', 'models', 'num_threads', 'sentence_case', 'final_padding_ms'}
                or not isinstance(provider['models'], dict) or set(provider['models']) != {'encoder','decoder','joiner','tokens'}
                or type(provider['num_threads']) is not int or not 1 <= provider['num_threads'] <= 2
                or type(provider['sentence_case']) is not bool
                or type(provider['final_padding_ms']) is not int or not 0 <= provider['final_padding_ms'] <= 2000):
            raise ValueError('Invalid local ASR configuration')
        for asset in provider['models'].values():
            if (not isinstance(asset, dict) or set(asset) != {'path','sha256'}
                    or not isinstance(asset['path'], str) or not Path(asset['path']).is_absolute()
                    or not isinstance(asset['sha256'], str) or not re.fullmatch('[0-9a-f]{64}',asset['sha256'])):
                raise ValueError('Invalid local ASR asset')
    elif (set(provider) != {'type', 'model_name', 'api_key', 'language', 'mode'}
            or not isinstance(provider['model_name'], str) or not 1 <= len(provider['model_name']) <= 256
            or provider['model_name'].startswith(('your-', 'your_'))
            or not isinstance(provider['api_key'], str) or not provider['api_key'].strip()
            or len(provider['api_key']) > 4096 or '${' in provider['api_key']
            or provider['api_key'].startswith(('your-', 'your_'))
            or any(c in provider['api_key'] for c in '\x00\r\n')
            or not isinstance(provider['language'], str) or not re.fullmatch(r'[A-Za-z0-9-]{2,35}', provider['language'])
            or provider['mode'] not in ('VERBATIM', 'SMART')):
        raise ValueError('Invalid selected ASR provider or credential')
    if (not isinstance(vad, dict) or set(vad) != {'model_path', 'sha256', 'threshold', 'threshold_low', 'min_silence_duration_ms'}
            or not isinstance(vad['model_path'], str) or not Path(vad['model_path']).is_absolute()
            or not isinstance(vad['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', vad['sha256'])
            or any(type(vad[k]) not in (int, float) or not math.isfinite(vad[k]) for k in ('threshold', 'threshold_low'))
            or not 0 <= vad['threshold_low'] <= vad['threshold'] <= 1
            or type(vad['min_silence_duration_ms']) is not int or not 32 <= vad['min_silence_duration_ms'] <= 5000):
        raise ValueError('Invalid VAD configuration')
    return value


def load_bundle(path, worker_id):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('ASR configuration requires an absolute path')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o027:
            raise ValueError('ASR configuration requires private permissions')
        raw = stream.read(65537)
    from .llm_protocol import decode
    return validate_bundle(decode(raw, 65536), worker_id)
