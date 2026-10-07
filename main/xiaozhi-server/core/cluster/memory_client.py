"""Session-owned Memory scope and bounded completed-turn context."""
import asyncio
import copy
import logging
import re
import time
import uuid

from plugins_func.tool_schemas import MANAGE_MEMORY_FUNCTION_DESC
from . import memory_protocol as wire

LOGGER = logging.getLogger('xiaozhi.core.memory')


class SessionHistory:
    def __init__(self):
        self.messages = []

    def dialogue(self, text):
        return copy.deepcopy(self.messages) + [{'role':'user', 'content':text}]

    def complete(self, text, response):
        # Keep complete pairs only. Never preserve failed, aborted, wake-only
        # turns or tool-call envelopes whose results may be incomplete.
        response = response.encode()[:12000].decode('utf-8', errors='ignore')
        self.messages.extend(({'role':'user', 'content':text}, {'role':'assistant', 'content':response}))
        while len(self.messages) > 14 or len(wire.encode(self.messages, 100000)) > 24000:
            del self.messages[:2]

    def recent(self):
        return [message['content'].encode()[:2048].decode('utf-8', errors='ignore') for message in self.messages[-6:]]


class MemoryClient:
    def __init__(self, rpc, binding, device_id, nodes):
        self.rpc, self.binding, self.device_id, self.nodes = rpc, wire.validate_bundle(binding), device_id, set(nodes)
        if not re.fullmatch('[0-9a-f]{2}(?::[0-9a-f]{2}){5}', device_id):
            raise ValueError('Memory requires an authenticated device identity')
        self.project = self.writer = None

    def tools(self):
        if not self.binding['tool_enabled']:
            return []
        description = copy.deepcopy(MANAGE_MEMORY_FUNCTION_DESC)
        function = description['function']
        function['description'] = function['description'].replace('durable local memory', 'durable shared Cloud Memory').replace(
            'remember or forget information', 'remember, update, delete or forget information')
        properties = function['parameters']['properties']
        properties['action']['enum'].extend(['update', 'delete'])
        properties['content']['description'] = 'Fact to save/update, or query for recall/forget. Omit for list/delete.'
        properties['entry_id'] = {'type': 'string', 'description':
            'Required for update/delete. Use an exact id returned by list/recall; never guess an id.'}
        return [description]

    async def call(self, action, arguments, history):
        seconds = 2 if action == 'context' else 18
        value = {'protocol':wire.PROTOCOL, 'request_id':uuid.uuid4().hex,
            'core_id':self.rpc.core_id, 'device_id':self.device_id, 'policy':self.binding['policy'],
            'deadline_ms':int(time.time()*1000) + seconds*1000, 'action':action, 'arguments':arguments,
            'context':{'recent_messages':history.recent(), 'active_project':self.project}}
        payload = wire.encode(value, wire.MAX_BYTES)
        wire.request(payload)
        target = self.rpc.core_id
        client = self.rpc.client
        async def one(node):
            if not client.is_connected:
                raise ConnectionError
            message = await client.request(wire.subject(node), payload, timeout=seconds)
            if not client.is_connected:
                raise ConnectionError
            result = wire.reply(message.data, value['request_id'])
            writer = result['writer_node_id']
            if writer is not None and writer not in self.nodes:
                raise ValueError('Unknown Memory writer')
            self.writer = writer
            return result
        # Every cluster control plane can write the common CAS authority.
        # Never replay a mutation after timeout/disconnect or a CAS conflict.
        async with asyncio.timeout(seconds):
            result = await one(target)
        if result['status'] != 'ok':
            raise ValueError(result['error'])
        self.project = result['active_project']
        return result['text']

    async def recall(self, text, history):
        try:
            return await self.call('context', {'content':text}, history)
        except Exception:
            LOGGER.warning('Memory recall unavailable; core_id=%s', self.rpc.core_id)
            return 'Saved memory is currently unavailable. Do not claim to recall saved personal facts.'

    async def execute(self, call, history):
        try:
            if not self.binding['tool_enabled'] or not isinstance(call['arguments'], dict):
                raise ValueError('Memory tool is unavailable')
            args = copy.deepcopy(call['arguments'])
            action = args.pop('action', None)
            if action not in {'remember', 'recall', 'forget', 'list', 'update', 'delete'}:
                raise ValueError('Invalid Memory action')
            text = await self.call(action, args, history)
            result = {'isError':False, 'content':[{'type':'text', 'text':text}]}
        except Exception:
            LOGGER.warning('Memory tool unavailable; core_id=%s', self.rpc.core_id)
            result = {'isError':True, 'error':'Memory operation could not be confirmed. Do not claim it was saved or deleted, and do not automatically retry.'}
        return {'id':call['id'], 'result':result}
