"""Bounded device-scoped explicit Memory RPC on the authenticated private bus."""
import copy
import hashlib
import json
import re
import time

from .llm_protocol import decode, encode, identity
from .nats_config import validate_worker_id

PROTOCOL = 'xiaozhi-memory-rpc-v1'
CHANGED = 'xiaozhi.v1.memory.changed'
MAX_BYTES = 16384
MUTATIONS = {'remember', 'forget', 'update', 'delete'}
ERRORS = {'memory_unavailable', 'memory_read_only', 'memory_conflict',
          'memory_policy_mismatch', 'memory_invalid', 'memory_busy', 'memory_expired'}


def policy(config):
    selected = config.get('selected_module', {}).get('Memory', 'nomem')
    source = copy.deepcopy(config.get('Memory', {}).get(selected, {}))
    kind = source.get('type', selected)
    if kind == 'nomem':
        return None, None
    if kind != 'mem_local_explicit':
        raise ValueError('Distributed Memory supports nomem or mem_local_explicit')
    source['type'] = kind
    encode(source, MAX_BYTES)
    digest = hashlib.sha256(json.dumps(source, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    return source, digest


def export_config(config):
    source, digest = policy(config)
    if source is None:
        return None
    intent = config.get('Intent', {}).get(config.get('selected_module', {}).get('Intent'), {})
    return {'type': 'mem_local_explicit', 'policy': digest,
            'tool_enabled': intent.get('type') == 'function_call' and 'manage_memory' in intent.get('functions', [])}


def validate_bundle(value):
    if (not isinstance(value, dict) or set(value) != {'type', 'policy', 'tool_enabled'}
            or value['type'] != 'mem_local_explicit' or type(value['tool_enabled']) is not bool
            or not isinstance(value['policy'], str) or not re.fullmatch('[0-9a-f]{64}', value['policy'])):
        raise ValueError('Invalid immutable Memory binding')
    return value


def subject(node):
    return 'xiaozhi.v1.memory.' + validate_worker_id(node)


def request(data):
    value = decode(data, MAX_BYTES)
    if (not isinstance(value, dict) or set(value) != {'protocol', 'request_id', 'core_id',
            'device_id', 'policy', 'deadline_ms', 'action', 'arguments', 'context'}
            or value['protocol'] != PROTOCOL or not isinstance(value['device_id'], str)
            or not re.fullmatch('[0-9a-f]{2}(?::[0-9a-f]{2}){5}', value['device_id'])
            or not isinstance(value['policy'], str) or not re.fullmatch('[0-9a-f]{64}', value['policy'])
            or type(value['deadline_ms']) is not int or value['deadline_ms'] > time.time()*1000 + 21000
            or value['action'] not in {'context', 'remember', 'recall', 'forget', 'list', 'update', 'delete'}
            or not isinstance(value['arguments'], dict) or not isinstance(value['context'], dict)
            or set(value['context']) != {'recent_messages', 'active_project'}):
        raise ValueError('Invalid Memory request')
    identity(value['request_id']); validate_worker_id(value['core_id'])
    args = value['arguments']
    allowed = {'content', 'type', 'project', 'entities', 'tags', 'importance', 'pinned', 'active', 'supersedes'}
    if value['action'] == 'update':
        allowed |= {'entry_id'}
    elif value['action'] == 'delete':
        allowed = {'entry_id'}
    elif value['action'] != 'remember':
        allowed = {'content'}
    if set(args) - allowed:
        raise ValueError('Invalid Memory arguments')
    if value['action'] not in {'list', 'delete'} and (not isinstance(args.get('content'), str)
            or not args['content'].strip() or len(args['content'].encode()) > 2048):
        raise ValueError('Invalid Memory content')
    if value['action'] == 'list' and args:
        raise ValueError('List accepts no arguments')
    if value['action'] in {'update', 'delete'} and (not isinstance(args.get('entry_id'), str)
            or not re.fullmatch('[a-zA-Z0-9_-]{1,128}', args['entry_id'])):
        raise ValueError('Invalid Memory entry identity')
    for key in ('type', 'project', 'supersedes'):
        if key in args and (not isinstance(args[key], str) or len(args[key].encode()) > 300):
            raise ValueError('Invalid Memory metadata')
    for key in ('entities', 'tags'):
        if key in args and (not isinstance(args[key], list) or len(args[key]) > 32 or any(
                not isinstance(item, str) or len(item.encode()) > 300 for item in args[key])):
            raise ValueError('Invalid Memory metadata list')
    if 'importance' in args and (type(args['importance']) is not int or not 1 <= args['importance'] <= 5):
        raise ValueError('Invalid Memory importance')
    for key in ('pinned', 'active'):
        if key in args and type(args[key]) is not bool:
            raise ValueError('Invalid Memory boolean')
    recent, project = value['context']['recent_messages'], value['context']['active_project']
    if (not isinstance(recent, list) or len(recent) > 6 or any(not isinstance(t, str)
            or len(t.encode()) > 2048 for t in recent) or project is not None and (
            not isinstance(project, str) or len(project.encode()) > 300)):
        raise ValueError('Invalid Memory recall context')
    return value


def reply(data, request_id):
    value = decode(data, MAX_BYTES)
    if (not isinstance(value, dict) or set(value) != {'protocol', 'request_id', 'status', 'text',
            'active_project', 'writer_node_id', 'memory_revision', 'error'}
            or value['protocol'] != PROTOCOL or value['request_id'] != request_id
            or value['status'] not in {'ok', 'error'} or not isinstance(value['text'], str)
            or len(value['text'].encode()) > 10000
            or value['active_project'] is not None and (not isinstance(value['active_project'], str)
                or len(value['active_project'].encode()) > 300)
            or value['memory_revision'] is not None and (type(value['memory_revision']) is not int
                or value['memory_revision'] < 1)
            or value['error'] not in (ERRORS if value['status'] == 'error' else {None})):
        raise ValueError('Invalid Memory reply')
    if value['writer_node_id'] is not None:
        validate_worker_id(value['writer_node_id'])
    return value
