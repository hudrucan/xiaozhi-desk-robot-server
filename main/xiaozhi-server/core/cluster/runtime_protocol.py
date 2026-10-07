"""Small metadata-only commands for the deployment-owned runtime apply agent."""
import json
import re

PROTOCOL = 'xiaozhi-runtime-apply-v1'
MAX_BYTES = 8192
ACTIONS = {'status', 'job', 'check', 'prepare', 'quiesce', 'install', 'worker', 'core', 'finish',
           'restore', 'old_worker', 'old_core', 'rolled_back'}
TERMINAL = {'complete', 'rolled_back'}


def decode(data):
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_BYTES:
        raise ValueError('Invalid runtime message size')
    def unique(pairs):
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError('Duplicate runtime field')
        return value
    return json.loads(data, object_pairs_hook=unique)


def command(value):
    if (not isinstance(value, dict) or set(value) != {'action', 'operation', 'revision', 'request_id'}
            or value['action'] not in ACTIONS
            or not isinstance(value['operation'], str)
            or not re.fullmatch('[0-9a-f]{32}', value['operation'])
            or not isinstance(value['request_id'], str) or not re.fullmatch('[0-9a-f]{32}', value['request_id'])
            or type(value['revision']) is not int or not 1 <= value['revision'] < 2**53):
        raise ValueError('Invalid runtime command')
    return value


def safe_node(value, node):
    """Never forward paths, bundle bodies, fingerprints of secrets or exceptions."""
    keys = {'node_id', 'state', 'operation', 'revision', 'previous_revision',
            'worker_revision', 'core_revision', 'ready', 'candidate_fingerprint', 'code'}
    if not isinstance(value, dict) or set(value) != keys or value['node_id'] != node:
        raise ValueError('Invalid runtime status')
    if value['state'] not in {'idle', 'prepared', 'quiesced', 'installed', 'worker_started',
                              'core_started', 'complete', 'restored', 'old_worker_started',
                              'old_core_started', 'rolled_back'}:
        raise ValueError('Invalid runtime phase')
    for key in ('revision', 'previous_revision', 'worker_revision', 'core_revision'):
        if value[key] is not None and (type(value[key]) is not int or not 1 <= value[key] < 2**53):
            raise ValueError('Invalid runtime revision')
    for key, length in (('operation', 32), ('candidate_fingerprint', 64)):
        if value[key] is not None and (not isinstance(value[key], str)
                or not re.fullmatch('[0-9a-f]{' + str(length) + '}', value[key])):
            raise ValueError('Invalid runtime identity')
    if type(value['ready']) is not bool or value['code'] not in (None, 'runtime_unavailable'):
        raise ValueError('Invalid runtime health')
    return {key: value[key] for key in keys}


def safe_job(value):
    keys = {'state', 'phase', 'node_id', 'revision', 'operation', 'code'}
    if (not isinstance(value, dict) or set(value) != keys
            or value['state'] not in {'idle', 'running', 'complete', 'failed', 'rolled_back', 'recovery_required'}
            or value['phase'] not in ACTIONS | {'preparing', 'recovering', 'verified', 'rolled_back', None}
            or value['code'] not in {None, 'runtime_apply_failed', 'runtime_recovery_required'}
            or (value['node_id'] is not None and (not isinstance(value['node_id'], str)
                or not re.fullmatch('[A-Za-z0-9_-]{1,192}', value['node_id'])))):
        raise ValueError('Invalid runtime job')
    if value['revision'] is not None and (type(value['revision']) is not int or not 1 <= value['revision'] < 2**53):
        raise ValueError('Invalid runtime job revision')
    if value['operation'] is not None and (not isinstance(value['operation'], str)
            or not re.fullmatch('[0-9a-f]{32}', value['operation'])):
        raise ValueError('Invalid runtime job identity')
    return dict(value)
