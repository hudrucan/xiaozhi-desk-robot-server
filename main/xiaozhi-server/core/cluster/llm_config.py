"""Explicit, node-local immutable LLM runtime bundle; never sent over NATS."""
import json
import os
import stat
from pathlib import Path

from .nats_config import validate_worker_id

MAX_CONFIG_BYTES = 65536
FIELDS = {'type', 'model_name', 'api_key', 'max_output_tokens', 'thinking_level',
          'temperature', 'top_p', 'top_k', 'timeout'}


def validate_bundle(value, worker_id):
    validate_worker_id(worker_id)
    if (not isinstance(value, dict) or set(value) != {'protocol', 'worker_id', 'revision', 'prompt', 'provider'}
            or value['protocol'] != 'xiaozhi-worker-llm-config-v1' or value['worker_id'] != worker_id
            or type(value['revision']) is not int or value['revision'] < 1
            or not isinstance(value['prompt'], str) or len(value['prompt'].encode('utf-8')) > 16384
            or not isinstance(value['provider'], dict) or set(value['provider']) - FIELDS):
        raise ValueError('Invalid local LLM configuration')
    provider = value['provider']
    # First implementation is Gemini. Other provider adapters may implement the
    # same cancellable text interface; Core/NATS never chooses a provider by name.
    if provider.get('type') != 'gemini':
        raise ValueError('Configured LLM adapter is not supported yet')
    key = provider.get('api_key')
    if (not isinstance(key, str) or not key.strip() or '${' in key or '\u4f60' in key
            or key.lower().startswith(('your_', 'your-')) or len(key) > 4096
            or any(c in key for c in '\x00\r\n')):
        raise ValueError('Required local LLM credential is unavailable')
    if not isinstance(provider.get('model_name'), str) or not 1 <= len(provider['model_name']) <= 256:
        raise ValueError('Invalid configured model')
    timeout = provider.get('timeout', 30)
    tokens = provider.get('max_output_tokens', 2048)
    if type(timeout) not in (int, float) or not 0 < timeout <= 30 or type(tokens) is not int or not 1 <= tokens <= 8192:
        raise ValueError('LLM runtime bounds require timeout <=30s and output tokens <=8192')
    # Validate serialization and finite numeric fields before provider creation.
    content = json.dumps(value, allow_nan=False).encode('utf-8')
    if len(content) > MAX_CONFIG_BYTES:
        raise ValueError('Local LLM configuration exceeds limit')
    return value


def load_bundle(path, worker_id):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink():
        raise ValueError('LLM configuration requires an explicit local regular file')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o027:
            raise ValueError('LLM configuration requires a private, non-group-writable regular file')
        data = source.read(MAX_CONFIG_BYTES + 1)
    if len(data) > MAX_CONFIG_BYTES:
        raise ValueError('Local LLM configuration exceeds limit')
    def unique(pairs):
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError('Duplicate local LLM fields')
        return value
    return validate_bundle(json.loads(data, object_pairs_hook=unique), worker_id)


def create_provider(bundle):
    from core.providers.llm.gemini.gemini import LLMProvider
    return LLMProvider(bundle['provider'])
