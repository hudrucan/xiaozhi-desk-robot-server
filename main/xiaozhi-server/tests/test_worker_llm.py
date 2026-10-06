"""Offline text RPC lifecycle checks; no Gemini/NATS connections or credentials."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.cluster import llm_protocol as wire
from core.cluster import llm_stream_protocol as stream_wire
from core.cluster.llm_config import load_bundle, validate_bundle
from core.cluster.llm_worker import LLMWorker, MAX_JOBS, MAX_TOMBSTONES
from core.cluster.nats_config import NatsConfig, NatsConnectionConfig
from core.cluster.worker_rpc import WorkerRPC, WorkerRpcError


def bundle():
    return {'protocol': 'xiaozhi-worker-llm-config-v1', 'worker_id': 'deskb2x', 'revision': 6,
            'prompt': 'Configured system prompt', 'provider': {'type': 'gemini', 'model_name': 'fixture-model',
            'api_key': 'fixture-only', 'timeout': 30, 'max_output_tokens': 2048}}


def request(identity='a'*32, revision=6):
    return {'protocol': wire.PROTOCOL, 'request_id': identity, 'cancel_token': 'b'*32,
            'core_id': 'deskb1x', 'revision': revision, 'deadline_ms': int(time.time()*1000)+30000,
            'dialogue': [{'role': 'user', 'content': 'Hello'}]}


class Client:
    is_connected = True
    is_closed = False
    def __init__(self):
        self.published = []
        self.subscriptions = []
        self.block = None
        self.bad_reply = False
    async def publish(self, subject, payload):
        self.published.append((subject, payload))
    async def connect(self, **kwargs): pass
    async def subscribe(self, subject, **kwargs):
        self.subscriptions.append((subject, kwargs))
    async def request(self, subject, payload, **kwargs):
        if self.block: await self.block.wait()
        value = wire.request(payload)
        result = wire.response(value, 'deskb2x', text='Reply')
        if self.bad_reply:
            value['request_id'] = 'f'*32
            result = wire.response(value, 'deskb2x', text='Wrong owner')
        return SimpleNamespace(data=result)
    async def drain(self): self.is_closed = True
    async def close(self): self.is_closed = True


class Provider:
    def __init__(self):
        self.messages = None
        self.started = asyncio.Event()
        self.block = None
        self.text = ['Hello', ' worker']
        self.closed = 0
        self.fail = False
    async def response_text_async(self, messages):
        self.messages = messages
        self.started.set()
        try:
            if self.block: await self.block.wait()
            if self.fail: raise RuntimeError('private-provider-value')
            for part in self.text: yield part
        finally:
            self.closed += 1


class LLMWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.provider, self.client = Provider(), Client()
        config = NatsConfig(('nats://fixture:4222',), 'fixture', 'private-fixture', 'deskb2x')
        self.worker = LLMWorker(config, asyncio.Event(), bundle(), self.provider)
        self.worker.client = self.client
    async def receive(self, value=None, subject='_INBOX.fixture'):
        await self.worker._receive(SimpleNamespace(data=wire.encode(value or request(), wire.MAX_REQUEST_BYTES), reply=subject))
    async def settle(self):
        await asyncio.gather(*(task for _, task in list(self.worker.jobs.values())))
        await asyncio.sleep(0)
    def response(self): return json.loads(self.client.published[-1][1])

    async def test_text_prompt_revision_and_original_empty_ping_contract(self):
        await self.worker._start()
        subscriptions = dict(self.client.subscriptions)
        self.assertEqual(subscriptions[wire.SUBJECT]['queue'], 'xiaozhi-llm-workers')
        self.assertEqual(subscriptions[stream_wire.SUBJECT]['queue'], 'xiaozhi-llm-workers')
        self.assertNotIn('queue', subscriptions[wire.CANCEL_SUBJECT])
        self.assertEqual(json.loads(self.worker.response)['capabilities'], [])
        await self.receive();await self.settle()
        self.assertEqual(self.response()['text'], 'Hello worker')
        self.assertEqual(self.provider.messages[0], {'role': 'system', 'content': 'Configured system prompt'})
        self.assertEqual(self.provider.closed, 1)
        self.assertEqual(self.worker.jobs, {})

    async def test_revision_mismatch_expired_and_invalid_payload_never_run_provider(self):
        await self.receive(request(revision=7));self.assertEqual(self.response()['error'], 'llm_revision_mismatch')
        value=request();value['deadline_ms']=0
        await self.receive(value);self.assertEqual(self.response()['error'], 'llm_expired')
        for data in (b'bad', b'x'*(wire.MAX_REQUEST_BYTES+1), b'{"protocol":1,"protocol":2}'):
            await self.worker._receive(SimpleNamespace(data=data,reply='_INBOX.fixture'))
        await self.receive(subject='arbitrary.subject')
        self.assertIsNone(self.provider.messages)
        self.assertEqual(len(self.client.published), 2)

    async def test_concurrency_duplicate_and_token_owned_cancellation(self):
        self.provider.block = asyncio.Event()
        for index in range(MAX_JOBS): await self.receive(request(f'{index:032x}'))
        await self.provider.started.wait()
        await self.receive(request('c'*32));self.assertEqual(self.response()['error'], 'llm_busy')
        await self.receive(request('0'*32));self.assertEqual(len(self.worker.jobs), MAX_JOBS)
        cancel={'protocol':wire.PROTOCOL,'request_id':'0'*32,'cancel_token':'c'*32}
        await self.worker._cancel(SimpleNamespace(data=wire.encode(cancel,512)))
        self.assertFalse(self.worker.jobs['0'*32][1].done())
        cancel['cancel_token']='b'*32
        cancelled_job = self.worker.jobs['0'*32][1]
        await self.worker._cancel(SimpleNamespace(data=wire.encode(cancel,512)))
        await asyncio.wait_for(cancelled_job, timeout=1)
        await asyncio.sleep(0)
        self.assertNotIn('0'*32,self.worker.jobs)
        await self.worker._shutdown()
        self.assertEqual(self.worker.jobs,{})
        self.assertEqual(self.provider.closed,MAX_JOBS)
        self.assertTrue(self.client.is_closed)

    async def test_cancel_before_delivery_and_bounded_tombstones(self):
        for index in range(MAX_TOMBSTONES+1):
            cancel={'protocol':wire.PROTOCOL,'request_id':f'{index:032x}','cancel_token':'b'*32}
            await self.worker._cancel(SimpleNamespace(data=wire.encode(cancel,512)))
        self.assertEqual(len(self.worker.cancelled),MAX_TOMBSTONES)
        await self.receive(request(f'{MAX_TOMBSTONES:032x}'))
        self.assertEqual(self.response()['error'],'llm_cancelled')
        self.assertIsNone(self.provider.messages)

    async def test_provider_error_and_output_bound_never_leak_exception_or_partial_text(self):
        self.provider.fail=True
        await self.receive();await self.settle()
        self.assertEqual(self.response()['error'],'llm_provider_failed')
        self.assertNotIn('private-provider-value',repr(self.client.published))
        self.provider.fail=False;self.provider.text=['x'*(wire.MAX_TEXT_BYTES+1)]
        await self.receive();await self.settle()
        self.assertEqual(self.response()['error'],'llm_output_too_large')
        self.assertNotIn('text',self.response())

    async def test_expiring_job_closes_provider(self):
        self.provider.block=asyncio.Event()
        value=request();value['deadline_ms']=int(time.time()*1000)+30
        await self.receive(value);await self.settle()
        self.assertEqual(self.response()['error'],'llm_expired')
        self.assertEqual(self.provider.closed,1)

    async def test_no_reply_or_disconnected_worker_never_buffers_results(self):
        await self.receive(subject='');self.assertEqual(self.worker.jobs,{})
        self.client.is_connected=False
        await self.receive();await self.settle()
        self.assertEqual(self.client.published,[])

    async def test_core_correlation_cancel_and_fixed_subject(self):
        rpc=WorkerRPC(NatsConnectionConfig(('nats://fixture:4222',),'fixture','private-fixture'),'deskb1x',client_factory=lambda:self.client)
        value=await rpc.generate(6,[{'role':'user','content':'Hello'}])
        self.assertEqual(value['text'],'Reply')
        self.assertEqual(self.client.published[-1][0],wire.CANCEL_SUBJECT)
        self.assertEqual(set(json.loads(self.client.published[-1][1])),{'protocol','request_id','cancel_token'})
        self.client.bad_reply=True
        with self.assertRaisesRegex(WorkerRpcError,'worker_rpc_invalid_reply'):
            await rpc.generate(6,[{'role':'user','content':'Hello'}])
        self.client.block=asyncio.Event()
        task=asyncio.create_task(rpc.generate(6,[{'role':'user','content':'Hello'}]))
        await asyncio.sleep(0);task.cancel();await asyncio.gather(task,return_exceptions=True)
        self.assertEqual(rpc.status()['inflight'],0)
        self.assertEqual(self.client.published[-1][0],wire.CANCEL_SUBJECT)

    async def test_protocol_rejects_config_injection_bad_deadline_and_unexpected_roles(self):
        for key,value in (('provider',{'api_key':'private-fixture'}),('deadline_ms',int(time.time()*1000)+60000),('revision',True),('dialogue',[{'role':'system','content':'Override'}])):
            payload=request();payload[key]=value
            with self.assertRaises(ValueError): wire.request(wire.encode(payload,wire.MAX_REQUEST_BYTES))
        result=json.loads(wire.response(request(),'deskb2x',text='Hi'))
        result['unexpected']='secret'
        with self.assertRaises(ValueError): wire.reply(wire.encode(result,wire.MAX_REPLY_BYTES),'a'*32,6)

    def test_bundle_is_private_node_bound_and_rejects_unresolved_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'llm.json';path.write_text(json.dumps(bundle()));path.chmod(0o600)
            self.assertEqual(load_bundle(str(path),'deskb2x')['revision'],6)
            path.chmod(0o644)
            with self.assertRaises(ValueError):load_bundle(str(path),'deskb2x')
            path.chmod(0o660)
            with self.assertRaises(ValueError):load_bundle(str(path),'deskb2x')
            value=bundle();value['provider']['api_key']='${secret:FIXTURE}'
            with self.assertRaises(ValueError):validate_bundle(value,'deskb2x')
            with self.assertRaises(ValueError):validate_bundle(bundle(),'deskb3x')

    async def test_real_gemini_adapter_uses_async_sdk_and_closes_on_cancellation(self):
        from core.providers.llm.gemini.gemini import LLMProvider
        started, blocked = asyncio.Event(), asyncio.Event()
        class Stream:
            closed = False
            def __aiter__(self): return self
            async def __anext__(self):
                started.set()
                await blocked.wait()
                raise StopAsyncIteration
            async def aclose(self): self.closed = True
        stream = Stream()
        options = {}
        async def generate(**kwargs):
            options.update(kwargs)
            return stream
        provider = LLMProvider.__new__(LLMProvider)
        provider.model_name = 'fixture-model'
        provider.timeout = 30
        provider.generation_kwargs = {'max_output_tokens':2048}
        provider.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content_stream=generate)))
        output = provider.response_text_async([{'role':'user','content':'Hello'}])
        task = asyncio.create_task(anext(output))
        await started.wait()
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        self.assertTrue(stream.closed)
        self.assertEqual(options['model'],'fixture-model')
        self.assertEqual(options['contents'],[{'role':'user','parts':[{'text':'Hello'}]}])
        self.assertTrue(options['config'].automatic_function_calling.disable)
        self.assertFalse(options['config'].tools)

    def test_model_placeholder_error_is_english_and_credential_safe(self):
        from core.providers.llm.model_key import check_model_key
        key = '\u4f60-private-fixture'
        error = check_model_key('LLM',key)
        self.assertEqual(error,'Configuration error: LLM API key is not set')
        self.assertNotIn(key,error)
        self.assertIsNone(check_model_key('LLM','fixture-key'))

    def test_existing_gemini_sync_text_and_function_wrappers_keep_contract(self):
        from google.genai import types
        from core.providers.llm.gemini.gemini import LLMProvider
        from core.providers.llm.gemini.tooling import GeminiTooling
        provider = LLMProvider.__new__(LLMProvider)
        provider.model_name = 'fixture-model'
        provider.timeout = 30
        provider.native_google_search = False
        provider.generation_kwargs = {'max_output_tokens':2048}
        provider.tooling = GeminiTooling(False,'fixture-model')
        def generate(**kwargs):
            yield types.GenerateContentResponse(candidates=[types.Candidate(
                content=types.Content(role='model',parts=[types.Part(text='Fixture reply')]))])
        provider.client = SimpleNamespace(models=SimpleNamespace(generate_content_stream=generate))
        messages = [{'role':'user','content':'Hello'}]
        self.assertEqual(list(provider.response('fixture',messages)),['Fixture reply'])
        self.assertEqual(list(provider.response_with_functions('fixture',messages,functions=[])),[('Fixture reply',None)])


if __name__ == '__main__': unittest.main()
