"""Offline device discovery and owned distributed tool continuation checks."""
import asyncio
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.cluster import tool_stream_protocol as wire
from core.cluster.device_mcp import DeviceMCP
from core.cluster.llm_worker import LLMWorker
from core.cluster.nats_config import NatsConfig, NatsConnectionConfig
from core.cluster.worker_rpc import WorkerRPC, WorkerRpcError
from test_worker_llm_stream import Bus

RAW = {'name':'self.robot.get_status','description':'Read robot state',
       'inputSchema':{'type':'object','properties':{},'required':[]}}
TOOL = {'type':'function','function':{'name':'self_robot_get_status',
        'description':RAW['description'],'parameters':RAW['inputSchema']}}
CALL = {'id':'fixture-call','name':'self_robot_get_status','arguments':{}}


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sent, self.events = [], []
        self.page = 0
        self.raw = [copy.deepcopy(RAW), {**RAW,'name':'self.robot.stop'}]
        async def send(message):
            self.sent.append(message)
            p = message['payload']
            if 'id' not in p: return
            if p['method'] == 'initialize': result = {'protocolVersion':'2024-11-05'}
            elif p['method'] == 'tools/list':
                index = 0 if not p['params'].get('cursor') else 1
                result = {'tools':[self.raw[index]]}
                if index == 0: result['nextCursor'] = self.raw[1]['name']
            else: result = {'content':[{'type':'text','text':'First'},{'type':'text','text':'Second'}]}
            self.mcp.receive({'jsonrpc':'2.0','id':p['id'],'result':result})
        self.mcp = DeviceMCP(send, lambda event, **fields:self.events.append(event))
        self.mcp.start()
    async def asyncTearDown(self): await self.mcp.close()
    async def test_paginated_inventory_aliases_and_all_result_blocks(self):
        tools = await self.mcp.tools()
        self.assertEqual(len(tools),2)
        self.assertEqual(tools[0], TOOL)
        self.assertEqual(self.sent[0]['payload']['params']['capabilities'],{})
        result = await self.mcp.execute([CALL])
        self.assertEqual([v['text'] for v in result[0]['result']['content']],['First','Second'])
        self.assertEqual(self.sent[-1]['payload']['params']['name'],RAW['name'])
        self.assertTrue(all(m['payload'].get('id',0) != 10000 for m in self.sent))
        self.assertEqual(self.mcp.pending,{})
        self.assertIn('mcp_ready',self.events)
    async def test_unknown_tool_never_sent_and_close_cancels_waiters(self):
        await self.mcp.tools()
        count = len(self.sent)
        result = await self.mcp.execute([{**CALL,'name':'not_advertised'}])
        self.assertTrue(result[0]['result']['isError'])
        self.assertEqual(len(self.sent),count)
        async def discard(message): pass
        self.mcp.send = discard
        task = asyncio.create_task(self.mcp.request('tools/call',{}))
        await asyncio.sleep(0)
        await self.mcp.close()
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(task.cancelled())
        self.assertFalse(self.mcp.pending)
    async def test_foreign_duplicate_and_late_results_do_not_resolve_another_call(self):
        await self.mcp.tools()
        sent = asyncio.Event()
        async def discard(message): sent.set()
        self.mcp.send = discard
        task = asyncio.create_task(self.mcp.request('tools/call',{}))
        await sent.wait()
        identity = next(iter(self.mcp.pending))
        self.mcp.receive({'jsonrpc':'2.0','id':identity+1,'result':{}})
        self.mcp.receive({'jsonrpc':'2.0','id':True,'result':{}})
        self.assertFalse(task.done())
        self.mcp.receive({'jsonrpc':'2.0','id':identity,'result':{'value':'owned'}})
        self.mcp.receive({'jsonrpc':'2.0','id':identity,'result':{'value':'duplicate'}})
        self.assertEqual(await task,{'value':'owned'})
    async def test_discovery_rejects_alias_collisions_and_repeated_cursor(self):
        await self.mcp.close()
        for raw in ([RAW,{**RAW,'name':'self_robot_get_status'}],[RAW]):
            async def send(message):
                p = message['payload']
                if 'id' not in p:return
                result = {'protocolVersion':'2024-11-05'} if p['method']=='initialize' else {'tools':raw,'nextCursor':'repeat'}
                self.mcp.receive({'jsonrpc':'2.0','id':p['id'],'result':result})
            self.mcp = DeviceMCP(send,lambda *a,**kw:None)
            self.mcp.start()
            with self.assertRaises(WorkerRpcError):await self.mcp.tools()
            await self.mcp.close()
    async def test_timeout_reports_uncertain_failure_without_retry(self):
        await self.mcp.tools()
        async def timeout(*a,**kw): raise asyncio.TimeoutError
        self.mcp.request = timeout
        result = await self.mcp.execute([CALL])
        self.assertTrue(result[0]['result']['isError'])
        self.assertIn('not retried',result[0]['result']['error'])


class ToolProvider:
    def __init__(self): self.results = None; self.closed = 0; self.calls = [CALL]; self.rounds = 1
    async def response_tools_async(self, messages, tools):
        try:
            for _ in range(self.rounds):
                self.results = yield {'calls':self.calls}
            yield 'Status '
            yield 'confirmed'
        finally:self.closed += 1


class ToolStreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bus, self.provider = Bus(), ToolProvider()
        cfg=NatsConfig(('nats://fixture:4222',),'fixture','private-fixture','deskb2x')
        self.worker=LLMWorker(cfg,asyncio.Event(),{'revision':9,'prompt':'Configured prompt'},self.provider)
        self.worker.client=self.bus
        await self.bus.subscribe(wire.SUBJECT,cb=self.worker._receive_tools,queue=wire.QUEUE_GROUP)
        from core.cluster import llm_protocol
        await self.bus.subscribe(llm_protocol.CANCEL_SUBJECT,cb=self.worker._cancel)
        self.rpc=WorkerRPC(NatsConnectionConfig(cfg.servers,cfg.username,cfg.password),'deskb1x',client_factory=lambda:self.bus)
        self.called,self.chunks=[],[]
    async def asyncTearDown(self):
        await self.rpc.close();await self.worker._shutdown()
    async def execute(self,calls):
        self.called.extend(calls)
        return [{'id':c['id'],'result':{'content':[{'type':'text','text':'Fixture state'}]}} for c in calls]
    async def chunk(self,text,seq):self.chunks.append(text)
    async def call(self,**options):
        return await self.rpc.generate_stream(9,[{'role':'user','content':'Read status'}],self.chunk,
            tools=[TOOL],on_tools=options.get('on_tools',self.execute),seconds=options.get('seconds',30))
    async def test_one_worker_receives_results_then_resumes_llm_and_streaming_text(self):
        result=await self.call()
        self.assertEqual(result['worker_id'],'deskb2x')
        self.assertEqual(result['text'],'Status confirmed')
        self.assertEqual(self.called,[CALL])
        self.assertEqual(self.provider.results[0]['id'],CALL['id'])
        self.assertEqual(''.join(self.chunks),result['text'])
        self.assertEqual(self.rpc.status()['inflight'],0)
        kinds=[json.loads(data).get('kind') for _,data in self.bus.published]
        self.assertLess(kinds.index('tools'),kinds.index('chunk'))
    async def test_multiple_calls_keep_ids_order_and_second_llm_step(self):
        self.provider.calls=[CALL,{**CALL,'id':'second-call'}]
        await self.call()
        self.assertEqual([c['id'] for c in self.provider.results],['fixture-call','second-call'])
    async def test_unadvertised_tool_and_excess_rounds_do_not_execute(self):
        self.provider.calls=[{**CALL,'name':'unknown'}]
        with self.assertRaisesRegex(WorkerRpcError,'llm_provider_failed'):await self.call()
        self.assertFalse(self.called)
        self.provider.calls=[CALL];self.provider.rounds=5
        with self.assertRaisesRegex(WorkerRpcError,'llm_tool_limit'):await self.call()
        self.assertEqual(len(self.called),4)
    async def test_abort_during_tool_wait_closes_provider_and_drops_owned_inbox(self):
        entered=asyncio.Event()
        async def blocked(calls):entered.set();await asyncio.Event().wait()
        task=asyncio.create_task(self.call(on_tools=blocked))
        await asyncio.wait_for(entered.wait(),2);task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        await asyncio.gather(*(t for _,t in list(self.worker.jobs.values())))
        self.assertEqual(self.provider.closed,1)
        self.assertEqual(len(self.bus.entries),2)
        self.assertFalse(self.rpc.streams)
        self.assertFalse(self.worker.jobs)
    async def test_duplicate_tool_event_never_repeats_device_execution(self):
        publish=self.bus.publish
        async def duplicate(subject,data,reply=''):
            await publish(subject,data,reply)
            if json.loads(data).get('kind')=='tools':
                await publish(subject,data,reply)
        self.bus.publish=duplicate
        with self.assertRaises(WorkerRpcError):await self.call()
        self.assertLessEqual(len(self.called),1)

    async def test_bad_result_owner_and_revision_fail_without_continuation(self):
        async def wrong(calls):return [{'id':'wrong','result':{}}]
        with self.assertRaises(WorkerRpcError):await self.call(on_tools=wrong)
        self.assertIsNone(self.provider.results)
    async def test_disconnect_during_tool_wait_invalidates_execution_before_reply(self):
        async def disconnected(calls):
            await self.rpc._disconnected();await self.rpc._reconnected()
            return await self.execute(calls)
        with self.assertRaisesRegex(WorkerRpcError,'llm_stream_unavailable'):await self.call(on_tools=disconnected)
        self.assertIsNone(self.provider.results)
    def test_strict_protocol_rejects_secrets_oversize_and_wrong_correlation(self):
        import time
        value={'protocol':wire.PROTOCOL,'request_id':'a'*32,'cancel_token':'b'*32,'core_id':'deskb1x',
            'revision':9,'deadline_ms':int(time.time()*1000)+30000,'dialogue':[{'role':'user','content':'Hello'}],'tools':[TOOL]}
        wire.request(wire.rpc.encode(value,wire.MAX_REQUEST_BYTES))
        for key,val in (('api_key','secret'),('revision',True),('deadline_ms',int(time.time()*1000)+130000)):
            with self.assertRaises(ValueError):wire.request(wire.rpc.encode({**value,key:val},wire.MAX_REQUEST_BYTES))
        event=wire.parse_event(wire.event(value,'deskb2x','tools',1,calls=[CALL]),'a'*32,9)
        reply=wire.rpc.decode(wire.tool_reply(event,[{'id':CALL['id'],'result':{}}]),wire.MAX_EVENT_BYTES)
        reply['worker_id']='deskb3x'
        with self.assertRaises(ValueError):wire.parse_tool_reply(wire.rpc.encode(reply,wire.MAX_EVENT_BYTES),event)
        reply['worker_id']='deskb2x';reply['seq']=True
        with self.assertRaises(ValueError):wire.parse_tool_reply(wire.rpc.encode(reply,wire.MAX_EVENT_BYTES),event)
        with self.assertRaises(ValueError):wire.tools([TOOL,TOOL])


class CoreImportTests(unittest.TestCase):
    def test_transport_and_mcp_import_without_server_provider_runtime(self):
        import subprocess
        import sys
        script = "import sys; import core.cluster.transport_core, core.cluster.device_mcp; assert not any(name.startswith(('core.providers', 'core.connection', 'google.genai')) for name in sys.modules)"
        result = subprocess.run([sys.executable,'-c',script],capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)


class GeminiContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_signed_parts_and_multiple_results_stay_in_local_provider_context(self):
        from google.genai import types
        from core.providers.llm.gemini.gemini import LLMProvider
        from core.providers.llm.gemini.tooling import GeminiTooling
        signature=b'private-provider-signature'
        original=[types.Part(text='Internal reasoning',thought=True),types.Part(function_call=types.FunctionCall(
            name=CALL['name'],args={},id='provider-id'),thought_signature=signature),
            types.Part(function_call=types.FunctionCall(name=CALL['name'],args={},id='provider-id-2'))]
        options=[]
        class Stream:
            def __init__(self,parts):self.parts=parts;self.closed=False
            def __aiter__(self):return self.iterate()
            async def iterate(self):
                yield types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role='model',parts=self.parts))])
            async def aclose(self):self.closed=True
        streams=[Stream(original),Stream([types.Part(text='Completed')])]
        async def generate(**kw):options.append(copy.deepcopy(kw));return streams[len(options)-1]
        p=LLMProvider.__new__(LLMProvider);p.model_name='fixture-model';p.timeout=30;p.generation_kwargs={'max_output_tokens':512}
        p.tooling=GeminiTooling(False,p.model_name)
        p.client=SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content_stream=generate)))
        stream=p.response_tools_async([{'role':'system','content':'Configured prompt'},{'role':'user','content':'Status'}],[TOOL])
        event=await anext(stream)
        self.assertEqual(len(event['calls']),2)
        self.assertNotIn(signature.decode(),str(event))
        results=[{'id':c['id'],'result':{'content':[{'type':'text','text':'Fixture result'}]}} for c in event['calls']]
        self.assertEqual(await stream.asend(results),'Completed')
        with self.assertRaises(StopAsyncIteration):await anext(stream)
        contents=options[1]['contents']
        self.assertEqual(contents[1].parts,original)
        self.assertEqual(contents[1].parts[1].thought_signature,signature)
        self.assertEqual([p.function_response.id for p in contents[2].parts],['provider-id','provider-id-2'])
        self.assertTrue(all(s.closed for s in streams))
        self.assertEqual(options[0]['config'].system_instruction,'Configured prompt')
        self.assertTrue(options[0]['config'].automatic_function_calling.disable)

if __name__=='__main__':unittest.main()
