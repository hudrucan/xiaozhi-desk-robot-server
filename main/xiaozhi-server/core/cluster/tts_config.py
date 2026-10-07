"""Explicit immutable Sherpa segment bundles and cross-node voice identity."""
import hashlib
import json
import math
import os
import re
import stat
from pathlib import Path, PurePosixPath

from .llm_protocol import decode, encode
from .nats_config import validate_worker_id

PROTOCOL = 'xiaozhi-worker-tts-config-v1'
DEFAULT_ERROR_RESPONSE = 'Sorry, something went wrong. Please try again.'
DEFAULTS = {'type': 'sherpa', 'model': 'model.onnx', 'tokens': 'tokens.txt',
    'data_dir': 'espeak-ng-data', 'provider': 'cpu', 'num_threads': 2,
    'max_num_sentences': 1, 'speaker_id': 0, 'speed': 1.0, 'silence_scale': .2,
    'noise_scale': .667, 'noise_scale_w': .8, 'length_scale': 1.0,
    'volume_gain': 1.0, 'mastering': False, 'target_active_rms_dbfs': -11.0,
    'peak_ceiling_dbfs': -1.0, 'text_normalizer': None, 'number_language': None,
    'first_segment_chars': 0, 'split_on_all_punctuations': False,
    'buffer_full_response': False, 'keep_model_warm': True, 'debug': False,
    'max_retries': 0, 'correct_words': []}
MAX_BUNDLE = 512 * 1024


def relative_path(value):
    if (not isinstance(value, str) or not value or '\\' in value
            or any(ord(c) < 32 for c in value)
            or PurePosixPath(value).is_absolute() or any(p in ('', '.', '..') for p in value.split('/'))):
        raise ValueError('Invalid relative TTS asset')
    return value


def fingerprint(options, files):
    # Filesystem enumeration and JSON object ordering may differ between nodes.
    raw = json.dumps({'options': options, 'files': files}, sort_keys=True,
        ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(raw) > MAX_BUNDLE:
        raise ValueError('Oversized TTS voice identity')
    return hashlib.sha256(raw).hexdigest()


def validate_bundle(value, node_id):
    keys = {'protocol', 'worker_id', 'revision', 'workers', 'model_root', 'options', 'files', 'fingerprint'}
    if (not isinstance(value, dict) or not keys <= set(value)
            or set(value) - keys - {'soundbank', 'system_error_response', 'session'}
            or value['protocol'] != PROTOCOL or value['worker_id'] != node_id
            or type(value['revision']) is not int or value['revision'] < 1):
        raise ValueError('Invalid TTS bundle identity')
    response = value.get('system_error_response', DEFAULT_ERROR_RESPONSE)
    if not isinstance(response, str) or not response.strip() or len(response.encode('utf-8')) > 2048:
        raise ValueError('Invalid configured system error response')
    if 'session' in value:
        from .session_config import validate
        validate(value['session'])
    validate_worker_id(node_id)
    nodes, options, files = value['workers'], value['options'], value['files']
    if (not isinstance(nodes, list) or not 1 <= len(nodes) <= 3
            or any(not isinstance(n, str) for n in nodes) or len(set(nodes)) != len(nodes)):
        raise ValueError('Invalid TTS worker membership')
    for node in nodes:
        validate_worker_id(node)
    if (not isinstance(options, dict) or set(options) != set(DEFAULTS)
            or options['type'] != 'sherpa' or options['provider'] != 'cpu'
            or options['text_normalizer'] not in (None, '', 'none', 'vietnormalizer')):
        raise ValueError('Unsupported selected TTS configuration')
    for key in ('buffer_full_response', 'keep_model_warm', 'split_on_all_punctuations', 'mastering', 'debug'):
        if type(options[key]) is not bool:
            raise ValueError('Invalid TTS boolean')
    if options['buffer_full_response'] or not options['keep_model_warm'] or options['debug']:
        raise ValueError('Segment TTS requires streaming text, warm model and private diagnostics')
    for key, minimum, maximum in (('num_threads', 1, 8), ('max_num_sentences', 1, 8),
            ('speaker_id', 0, 255), ('first_segment_chars', 0, 512), ('max_retries', 0, 2)):
        if type(options[key]) is not int or not minimum <= options[key] <= maximum:
            raise ValueError('Invalid TTS integer')
    for key in ('speed', 'silence_scale', 'noise_scale', 'noise_scale_w', 'length_scale',
            'volume_gain', 'target_active_rms_dbfs', 'peak_ceiling_dbfs'):
        if type(options[key]) not in (int, float) or not math.isfinite(options[key]):
            raise ValueError('Invalid TTS numeric option')
    if (any(options[k] <= 0 for k in ('speed', 'length_scale'))
            or any(options[k] < 0 for k in ('silence_scale', 'noise_scale', 'noise_scale_w', 'volume_gain'))
            or not options['target_active_rms_dbfs'] < options['peak_ceiling_dbfs'] <= 0):
        raise ValueError('Invalid TTS synthesis/mastering option')
    if options['number_language'] is not None and (not isinstance(options['number_language'], str)
            or not re.fullmatch('[A-Za-z_-]{2,32}', options['number_language'])):
        raise ValueError('Invalid number language')
    words = options['correct_words']
    if (not isinstance(words, list) or len(words) > 256 or any(not isinstance(word, str)
            or len(word) > 512 or '|' not in word or not word.split('|', 1)[0] for word in words)):
        raise ValueError('Invalid TTS corrections')
    if (not isinstance(value['model_root'], str) or not Path(value['model_root']).is_absolute()
            or not isinstance(files, dict) or not 3 <= len(files) <= 2048):
        raise ValueError('Invalid TTS asset manifest')
    for name, checksum in files.items():
        relative_path(name)
        if not isinstance(checksum, str) or not re.fullmatch('[0-9a-f]{64}', checksum):
            raise ValueError('Invalid TTS asset checksum')
    for key in ('model', 'tokens', 'data_dir'):
        relative_path(options[key])
    if (options['model'] not in files or options['tokens'] not in files
            or not any(name.startswith(options['data_dir'] + '/') for name in files)
            or value['fingerprint'] != fingerprint(options, files)):
        raise ValueError('TTS voice/asset identity differs')
    if 'soundbank' in value:
        from .tts_soundbank import validate
        validate(value['soundbank'], value['revision'])
    encode(value, MAX_BUNDLE)
    return value


def load_bundle(path, node_id):
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise ValueError('TTS bundle requires an absolute path')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o027:
            raise ValueError('TTS bundle requires private permissions')
        return validate_bundle(decode(stream.read(MAX_BUNDLE + 1), MAX_BUNDLE), node_id)


def verify_assets(bundle):
    root = Path(bundle['model_root'])
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Local TTS model directory unavailable')
    sidecar = bundle['options']['model'] + '.json'
    if (root / sidecar).exists() != (sidecar in bundle['files']):
        raise ValueError('TTS speaker/model metadata differs from manifest')
    for name, expected in bundle['files'].items():
        path = root / name
        if (any(part.is_symlink() for part in (path, *path.parents) if part.is_relative_to(root))
                or not path.is_file() or not path.resolve().is_relative_to(root.resolve())
                or path.stat().st_size > 512 * 1024 * 1024):
            raise ValueError('Local TTS asset unavailable')
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
                raise ValueError('Local TTS asset checksum differs')
    actual = {p.relative_to(root).as_posix() for p in (root / bundle['options']['data_dir']).rglob('*') if p.is_file()}
    expected = {name for name in bundle['files'] if name.startswith(bundle['options']['data_dir'] + '/')}
    if actual != expected:
        raise ValueError('TTS phoneme data differs from manifest')
