"""Device tool schemas/results only; provider credentials/context stay local."""
import re
import time
from . import llm_protocol as rpc
from . import llm_stream_protocol as text
from .nats_config import validate_worker_id

PROTOCOL = 'xiaozhi-llm-tools-v1'
SUBJECT = 'xiaozhi.v1.llm.tools.stream'
QUEUE_GROUP = rpc.QUEUE_GROUP
MAX_REQUEST_BYTES = 262144
MAX_EVENT_BYTES = 131072
MAX_CHUNK_BYTES = text.MAX_CHUNK_BYTES
MAX_CHUNKS = text.MAX_CHUNKS
MAX_SECONDS = 120
MAX_TOOLS = 128
MAX_CALLS = 8
MAX_ROUNDS = 4
ACK_SECONDS = text.ACK_SECONDS
TOOL_SECONDS = 20
ERRORS = text.ERRORS | {'llm_tool_limit', 'llm_tools_unavailable'}
chunks = text.chunks
dialogue = rpc.dialogue


def tools(value):
    if not isinstance(value, list) or len(value) > MAX_TOOLS:
        raise ValueError('Invalid tool inventory')
    names = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {'type', 'function'} or item['type'] != 'function':
            raise ValueError('Invalid tool declaration')
        f = item['function']
        if (not isinstance(f, dict) or set(f) != {'name', 'description', 'parameters'}
                or not isinstance(f['name'], str) or not re.fullmatch('[A-Za-z_][A-Za-z0-9_-]{0,63}', f['name'])
                or f['name'] in names or not isinstance(f['description'], str)
                or len(f['description'].encode()) > 8192 or not isinstance(f['parameters'], dict)
                or f['parameters'].get('type') != 'object'):
            raise ValueError('Invalid tool schema')
        names.add(f['name'])
    rpc.encode(value, MAX_REQUEST_BYTES - 32768)
    return value


def calls(value):
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_CALLS:
        raise ValueError('Invalid tool calls')
    ids = set()
    for c in value:
        if (not isinstance(c, dict) or set(c) != {'id', 'name', 'arguments'}
                or not isinstance(c['id'], str) or not re.fullmatch('[A-Za-z0-9_-]{1,128}', c['id'])
                or c['id'] in ids or not isinstance(c['name'], str)
                or not re.fullmatch('[A-Za-z_][A-Za-z0-9_-]{0,63}', c['name'])
                or not isinstance(c['arguments'], dict)):
            raise ValueError('Invalid tool call')
        ids.add(c['id'])
    rpc.encode(value, MAX_EVENT_BYTES - 1024)
    return value


def request(data):
    v = rpc.decode(data, MAX_REQUEST_BYTES)
    expected = {'protocol','request_id','cancel_token','core_id','revision','deadline_ms','dialogue','tools'}
    if (not isinstance(v, dict) or not expected <= set(v) or set(v) - expected - {'memory_context'} or v['protocol'] != PROTOCOL
            or type(v['revision']) is not int or v['revision'] < 1
            or type(v['deadline_ms']) is not int
            or v['deadline_ms'] > int(time.time()*1000) + (MAX_SECONDS+1)*1000):
        raise ValueError('Invalid tool stream request')
    if 'memory_context' in v and (not isinstance(v['memory_context'], str)
            or len(v['memory_context'].encode()) > 10000):
        raise ValueError('Invalid Memory context')
    rpc.identity(v['request_id']); rpc.identity(v['cancel_token'])
    validate_worker_id(v['core_id']); rpc.dialogue(v['dialogue']); tools(v['tools'])
    return v


def parse_event(data, request_id, revision):
    v = rpc.decode(data, MAX_EVENT_BYTES)
    common = {'protocol','request_id','worker_id','revision','kind','seq'}
    if (not isinstance(v, dict) or v.get('protocol') != PROTOCOL or v.get('request_id') != request_id
            or type(v.get('revision')) is not int or v['revision'] != revision
            or not isinstance(v.get('worker_id'), str) or type(v.get('seq')) is not int
            or not 0 <= v['seq'] <= MAX_CHUNKS+1):
        raise ValueError('Invalid tool event correlation')
    validate_worker_id(v['worker_id'])
    if v.get('kind') == 'tools':
        if set(v) != common | {'calls'} or v['seq'] == 0:
            raise ValueError('Invalid tool event')
        calls(v['calls'])
    elif v.get('kind') == 'error':
        if set(v) != common | {'error'} or v['error'] not in ERRORS:
            raise ValueError('Invalid tool error')
    else:
        text.parse_event(rpc.encode({**v, 'protocol':text.PROTOCOL}, MAX_EVENT_BYTES), request_id, revision)
    return v


def event(value, worker_id, kind, seq, **fields):
    v = {k:value[k] for k in ('request_id','revision')}
    v.update(protocol=PROTOCOL, worker_id=worker_id, kind=kind, seq=seq, **fields)
    data = rpc.encode(v, MAX_EVENT_BYTES)
    parse_event(data, value['request_id'], value['revision'])
    return data


def ack(v):
    return rpc.encode({k:v[k] for k in ('protocol','request_id','worker_id','revision','seq')}, 1024)


def validate_ack(data, v):
    reply = rpc.decode(data, 1024)
    if not isinstance(reply, dict) or type(reply.get('seq')) is not int or type(reply.get('revision')) is not int or reply != rpc.decode(ack(v), 1024):
        raise ValueError('Invalid tool stream ACK')


def tool_reply(v, results):
    if not isinstance(results, list) or len(results) != len(v['calls']):
        raise ValueError('Invalid tool results')
    for call, result in zip(v['calls'], results):
        if (not isinstance(result, dict) or set(result) != {'id','result'}
                or result['id'] != call['id'] or not isinstance(result['result'], dict)):
            raise ValueError('Invalid tool result owner')
    return rpc.encode({**rpc.decode(ack(v), 1024), 'results':results}, MAX_EVENT_BYTES)


def parse_tool_reply(data, v):
    reply = rpc.decode(data, MAX_EVENT_BYTES)
    if not isinstance(reply, dict) or type(reply.get('seq')) is not int or type(reply.get('revision')) is not int or set(reply) != set(rpc.decode(ack(v),1024)) | {'results'}:
        raise ValueError('Invalid tool reply')
    if rpc.decode(tool_reply(v, reply['results']), MAX_EVENT_BYTES) != reply:
        raise ValueError('Invalid tool reply correlation')
    return reply['results']
