"""Bounded Core NATS image transfer; no provider configuration on the wire."""
import re

from .llm_protocol import decode, encode, identity
from .nats_config import validate_worker_id

PROTOCOL = 'xiaozhi-vision-v1'
SUBJECT = 'xiaozhi.v1.vision.admit'
QUEUE = 'xiaozhi-vision-workers'
MAX_IMAGE = 5 * 1024 * 1024
CHUNK = 128 * 1024
# Leave room for the HTTP JSON serialized again inside a firmware MCP text
# block, within the existing core's 8 KiB WebSocket message limit.
MAX_TEXT = 3500
SECONDS = 15
ERRORS = {'vision_busy', 'vision_expired', 'vision_revision_mismatch',
          'vision_invalid_request', 'vision_unavailable', 'vision_provider_failed'}


def target(worker):
    return 'xiaozhi.v1.vision.' + validate_worker_id(worker)


def request(data):
    value = decode(data, 4096)
    base = {'protocol', 'op', 'job_id', 'token'}
    if not isinstance(value, dict) or not base <= set(value) or value['protocol'] != PROTOCOL:
        raise ValueError('Invalid vision envelope')
    identity(value['job_id']); identity(value['token'])
    op = value['op']
    if op == 'admit':
        if set(value) != base | {'revision', 'deadline_ms', 'size', 'sha256', 'question'}:
            raise ValueError('Invalid vision admission')
        if (type(value['revision']) is not int or value['revision'] < 1
                or type(value['deadline_ms']) is not int
                or type(value['size']) is not int or not 0 < value['size'] <= MAX_IMAGE
                or not isinstance(value['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', value['sha256'])
                or not isinstance(value['question'], str) or not value['question'].strip()
                or len(value['question'].encode()) > 2048):
            raise ValueError('Invalid vision metadata')
    elif op in {'finish', 'cancel'}:
        if set(value) != base:
            raise ValueError('Invalid vision operation')
    else:
        raise ValueError('Invalid vision operation')
    return value


def response(data, job_id):
    value = decode(data, MAX_TEXT + 1024)
    base = {'protocol', 'job_id', 'worker_id', 'status'}
    if (not isinstance(value, dict) or not base <= set(value)
            or value['protocol'] != PROTOCOL or value['job_id'] != job_id):
        raise ValueError('Invalid vision response')
    validate_worker_id(value['worker_id'])
    extra = {'admitted':set(), 'chunk':{'offset'}, 'ok':{'text'}, 'error':{'error'}}.get(value['status'])
    if extra is None or set(value) != base | extra:
        raise ValueError('Invalid vision response fields')
    if value['status'] == 'error' and value['error'] not in ERRORS:
        raise ValueError('Invalid vision error')
    if value['status'] == 'chunk' and (type(value['offset']) is not int or not 0 <= value['offset'] <= MAX_IMAGE):
        raise ValueError('Invalid vision offset')
    if value['status'] == 'ok' and (not isinstance(value['text'], str)
            or not value['text'].strip() or len(value['text'].encode()) > MAX_TEXT):
        raise ValueError('Invalid vision result')
    return value


def jpeg(image):
    # The Desk camera contract uploads JPEG. Validate its container, never decode
    # an untrusted image in the control plane or provider-free core.
    return isinstance(image, (bytes, bytearray)) and 4 <= len(image) <= MAX_IMAGE and image[:3] == b'\xff\xd8\xff' and image[-2:] == b'\xff\xd9'
