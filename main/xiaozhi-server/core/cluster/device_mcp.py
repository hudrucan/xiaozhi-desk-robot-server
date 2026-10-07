"""Session-owned firmware MCP; no provider/runtime/config-loading imports."""
import asyncio
import copy
import re
import secrets
from . import llm_protocol as json_wire
from . import tool_stream_protocol as wire
from .worker_rpc import WorkerRpcError

MAX_MESSAGE = 8192
MAX_INVENTORY = wire.MAX_REQUEST_BYTES - 32768


class DeviceMCP:
    def __init__(self, send, record, *, vision=None, device_id='', client_id=''):
        self.send, self.record = send, record
        # Separate sessions and the gateway's prefetch IDs (starting at 10000).
        self.next_id = secrets.randbelow(1000000000) + 1000000
        self.pending = {}
        self.inventory = []
        self.names = {}
        self.ready = asyncio.Event()
        self.closed = False
        self.discovery = None
        self.valid = False
        self.vision, self.device_id, self.client_id = vision, device_id, client_id
        self.camera_active = self.camera_upload = False
        self.camera_consumed = False
        self.camera_question = None
        self.vision_tasks = set()

    async def cancel_vision(self):
        self.camera_active = False
        self.camera_question = None
        tasks = tuple(self.vision_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def start(self):
        self.discovery = asyncio.create_task(self.discover())

    async def request(self, method, params=None, seconds=wire.TOOL_SECONDS):
        if self.closed or len(self.pending) >= wire.MAX_CALLS:
            raise WorkerRpcError('llm_tools_unavailable')
        identity = self.next_id
        self.next_id += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[identity] = future
        payload = {'jsonrpc':'2.0', 'id':identity, 'method':method}
        if params is not None:
            payload['params'] = params
        try:
            json_wire.encode(payload, MAX_MESSAGE-128)
            await asyncio.wait_for(self.send({'type':'mcp', 'payload':payload}), 2)
            return await asyncio.wait_for(future, seconds)
        finally:
            self.pending.pop(identity, None)
            future.cancel()

    def receive(self, payload):
        if not isinstance(payload, dict) or payload.get('jsonrpc') != '2.0' or type(payload.get('id')) is not int:
            return
        pending = self.pending.get(payload['id'])
        if pending is None or pending.done():
            return  # Late, duplicate, foreign-session and unsolicited responses.
        if ('result' in payload) == ('error' in payload):
            return
        if 'error' in payload:
            pending.set_exception(WorkerRpcError('llm_tools_unavailable'))
        else:
            pending.set_result(payload['result'])

    async def discover(self):
        try:
            async with asyncio.timeout(10):
                initialized = await self.request('initialize', {
                    'protocolVersion':'2024-11-05', 'capabilities':{'vision':self.vision} if self.vision else {},
                    'clientInfo':{'name':'xiaozhi-cluster-core','version':'1.0.0'}})
                if not isinstance(initialized, dict) or initialized.get('protocolVersion') != '2024-11-05':
                    raise ValueError('Invalid initialization')
                await asyncio.wait_for(self.send({'type':'mcp', 'payload':{
                    'jsonrpc':'2.0', 'method':'notifications/initialized'}}), 2)
                cursor, seen, raw = None, set(), []
                while True:
                    page = await self.request('tools/list', {'cursor':cursor} if cursor else {})
                    if not isinstance(page, dict) or not isinstance(page.get('tools'), list):
                        raise ValueError('Invalid inventory page')
                    raw.extend(page['tools'])
                    if len(raw) > wire.MAX_TOOLS:
                        raise ValueError('Too many device tools')
                    json_wire.encode(raw, MAX_INVENTORY)
                    cursor = page.get('nextCursor')
                    if not cursor:
                        break
                    if not isinstance(cursor, str) or len(cursor.encode()) > 256 or cursor in seen or not page['tools']:
                        raise ValueError('Invalid inventory cursor')
                    seen.add(cursor)
                names, inventory = {}, []
                for item in raw:
                    if not isinstance(item, dict):
                        raise ValueError('Invalid device tool')
                    original = item.get('name')
                    if not isinstance(original, str) or not re.fullmatch('[A-Za-z_][A-Za-z0-9_.-]{0,63}', original):
                        raise ValueError('Invalid device tool name')
                    if original == 'self.camera.take_photo' and self.vision is None:
                        continue
                    name = re.sub('[^A-Za-z0-9_-]', '_', original)
                    if name in names:
                        raise ValueError('Ambiguous device tool alias')
                    names[name] = original
                    inventory.append({'type':'function','function':{'name':name,
                        'description':item.get('description',''), 'parameters':copy.deepcopy(item.get('inputSchema'))}})
                # Rewrite cross-tool references just as the normal server does.
                for item in inventory:
                    description = item['function']['description']
                    if isinstance(description, str):
                        for name, original in names.items():
                            description = description.replace(original, name)
                        item['function']['description'] = description
                wire.tools(inventory)
                self.inventory, self.names, self.valid = inventory, names, True
                self.record('mcp_ready')
        except asyncio.CancelledError:
            raise
        except Exception:
            self.record('mcp_unavailable', code='llm_tools_unavailable')
        finally:
            self.ready.set()

    async def tools(self):
        await asyncio.wait_for(self.ready.wait(), 10)
        if self.closed or not self.valid:
            raise WorkerRpcError('llm_tools_unavailable')
        return copy.deepcopy(self.inventory)

    async def call(self, call):
        if self.closed or not self.valid or call['name'] not in self.names:
            return {'id':call['id'], 'result':{'isError':True,'error':'Tool is not available in this device session'}}
        self.record('mcp_call_started')
        camera = self.names[call['name']] == 'self.camera.take_photo'
        try:
            if camera:
                question = call['arguments'].get('question')
                if (self.vision is None or self.camera_active or not isinstance(question, str)
                        or not question.strip() or len(question.encode()) > 2048):
                    raise ValueError('Camera capability is unavailable')
                self.camera_active, self.camera_question = True, question
                self.camera_consumed = False
            result = await self.request('tools/call', {'name':self.names[call['name']], 'arguments':call['arguments']})
            if not isinstance(result, dict) or not isinstance(result.get('content'), list):
                raise ValueError('Invalid tool result')
            # Keep all textual MCP blocks and the error flag. Images/resources
            # need a future vision adapter; never turn arbitrary binary into text.
            texts = [item['text'] for item in result['content'] if isinstance(item, dict)
                     and item.get('type') == 'text' and isinstance(item.get('text'), str)]
            safe = {'isError':result.get('isError') is True,
                    'content':[{'type':'text','text':text} for text in texts]}
            json_wire.encode(safe, 14336)
            self.record('mcp_call_complete')
            return {'id':call['id'], 'result':safe}
        except asyncio.CancelledError:
            raise
        except Exception:
            self.record('mcp_call_failed', code='llm_tools_unavailable')
            # Do not retry a timed-out actuator call: it may already have run.
            return {'id':call['id'], 'result':{'isError':True,'error':'Device tool failed or timed out; execution was not retried'}}
        finally:
            if camera:
                await self.cancel_vision()

    async def execute(self, calls):
        wire.calls(calls)
        # Firmware tools may depend on one another. Preserve advertised order.
        results = []
        for call in calls:
            results.append(await self.call(call))
        return results

    async def close(self):
        self.closed = True
        await self.cancel_vision()
        if self.discovery is not None:
            self.discovery.cancel()
        for pending in tuple(self.pending.values()):
            if not pending.done():
                pending.cancel()
        self.pending.clear()
        self.inventory, self.names = [], {}
        self.ready.set()
        if self.discovery is not None:
            await asyncio.gather(self.discovery, return_exceptions=True)
