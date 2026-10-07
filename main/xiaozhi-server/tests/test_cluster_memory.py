"""Disposable Cloud/NATS fixtures; no deployment, credentials or provider calls."""
import asyncio
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from config.cloud_memory import CloudMemoryStore
from core.cluster.memory_client import MemoryClient, SessionHistory
from core.cluster.memory_service import MemoryService
from core.cluster import memory_protocol as wire
from core.cluster.voice_turn import VoiceTurn
import test_cloud_memory as memory_fixtures
from test_worker_asr import Bus, AUDIO

MAC = '02:00:00:00:00:01'
OTHER = '02:00:00:00:00:02'
NODES = ['deskb1x', 'deskb2x', 'deskb3x']


class MemoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = memory_fixtures.CloudMemoryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.drive.seed({MAC:[memory_fixtures.entry(content='Camera uses shared I2C', pinned=True)],
                                OTHER:[memory_fixtures.entry('other', 'Other device fact', pinned=True)]}, writer=NODES[0])
        self.bus = Bus()
        self.services = []
        self.source = {**self.fixture.config, 'type':'mem_local_explicit'}
        config = {'selected_module':{'Memory':'explicit'}, 'Memory':{'explicit':self.source}}
        _, self.digest = wire.policy(config)
        self.binding = {'type':'mem_local_explicit', 'policy':self.digest, 'tool_enabled':True}
        for node in NODES:
            bootstrap = copy.deepcopy(self.fixture.bootstrap)
            bootstrap['node_id'] = node
            store = SimpleNamespace(bootstrap=bootstrap)
            reconciliation = SimpleNamespace(store=store, client=self.bus, config=SimpleNamespace(reconcile_interval=.01))
            service = MemoryService(reconciliation)
            service.backend = CloudMemoryStore(bootstrap, self.fixture.drive,
                self.fixture.directory / node, self.source, materialize=False, shared_writes=True)
            service.backend.sync()
            service.source, service.fingerprint, service.nodes, service.state = self.source, self.digest, set(NODES), 'ready'
            await service.register(self.bus)
            self.services.append(service)
        self.addAsyncCleanup(self.cleanup)
        self.rpc = SimpleNamespace(client=self.bus, core_id=NODES[1])
        self.client = MemoryClient(self.rpc, self.binding, MAC, NODES)
        self.history = SessionHistory()

    async def cleanup(self):
        for service in self.services:
            await service.stop()

    async def test_control_plane_cache_does_not_materialize_local_yaml(self):
        self.assertFalse(self.fixture.path.exists())
        await self.client.call('remember', {'content':'New fact'}, self.history)
        self.assertFalse(self.fixture.path.exists())
        self.assertTrue((self.fixture.directory / NODES[1] / 'current.json').exists())

    async def test_cached_recall_and_scope_do_not_read_drive_per_turn(self):
        self.fixture.drive.events.clear()
        self.assertIn('Camera uses shared I2C', await self.client.recall('camera', self.history))
        self.assertNotIn('Other device fact', await self.client.recall('camera', self.history))
        self.assertEqual(self.fixture.drive.events, [])
        other = MemoryClient(self.rpc, self.binding, OTHER, NODES)
        self.assertIn('Other device fact', await other.recall('camera', self.history))
        self.assertNotIn('Camera uses shared I2C', await other.recall('camera', self.history))

    async def test_every_node_writes_shared_manifest_with_one_cas(self):
        text = await self.client.call('remember', {'content':'The desk robot is blue'}, self.history)
        self.assertEqual(text, 'I will remember that.')
        self.assertEqual(self.fixture.drive.manifest['revision'], 2)
        self.assertEqual(self.fixture.drive.events.count('cas'), 1)
        subjects = [s for s,_ in self.bus.published if s.startswith('xiaozhi.v1.memory.') and s != wire.CHANGED]
        self.assertEqual(subjects, [wire.subject(NODES[1])])
        self.assertEqual(self.fixture.drive.manifest["schema_version"], 2)
        self.assertNotIn("writer_node_id", self.fixture.drive.manifest)
        self.assertTrue(all(service.wake.is_set() for service in self.services))
        await self.services[2].owned(self.services[2].backend.sync)
        third = MemoryClient(SimpleNamespace(client=self.bus, core_id=NODES[2]), self.binding, MAC, NODES)
        self.assertIn('The desk robot is blue', await third.call('list', {}, self.history))
        self.assertNotIn('The desk robot is blue', await MemoryClient(self.rpc,self.binding,OTHER,NODES).call('list',{},self.history))

    async def test_offline_last_good_recall_but_no_successful_write(self):
        self.fixture.drive.offline = True
        for service in self.services:
            service.backend.sync()
        self.assertIn('Camera uses shared I2C', await self.client.recall('camera', self.history))
        result = await self.client.execute({'id':'one','arguments':{'action':'remember','content':'New fact'}}, self.history)
        self.assertTrue(result['result']['isError'])
        self.assertEqual(self.fixture.drive.manifest['revision'], 1)
        self.assertEqual(self.fixture.drive.uploads, 0)

    async def test_duplicate_and_changed_request_id_never_replays_cas(self):
        client = MemoryClient(SimpleNamespace(client=self.bus,core_id=NODES[0]),self.binding,MAC,NODES)
        await client.call('remember',{'content':'New fact'},self.history)
        subject,payload = next((s,p) for s,p in self.bus.published if s == wire.subject(NODES[0]))
        message = await self.bus.request(subject,payload,timeout=1)
        self.assertEqual(wire.reply(message.data,json.loads(payload)['request_id'])['error'],'memory_busy')
        value=json.loads(payload);value['arguments']['content']='Another fact'
        await self.bus.request(subject,wire.encode(value,wire.MAX_BYTES),timeout=1)
        self.assertEqual(self.fixture.drive.events.count('cas'),1)

    async def test_uncertain_write_reply_is_not_retried(self):
        class LostReply:
            is_connected = True
            calls = 0
            async def request(inner, subject, data, timeout):
                inner.calls += 1
                value=wire.request(data)
                self.services[0].execute(value)
                raise ConnectionError('private message')
        lost=LostReply()
        client=MemoryClient(SimpleNamespace(client=lost,core_id=NODES[0]),self.binding,MAC,NODES)
        result=await client.execute({'id':'one','arguments':{'action':'remember','content':'New fact'}},self.history)
        self.assertTrue(result['result']['isError'])
        self.assertEqual(lost.calls,1)
        self.assertEqual(self.fixture.drive.manifest['revision'],2)
        self.assertNotIn('private message',str(result))

    async def test_cloud_conflict_does_not_retry_or_claim_success(self):
        self.fixture.drive.failure='cas'
        result=await self.client.execute({'id':'one','arguments':{'action':'remember','content':'New fact'}},self.history)
        self.assertTrue(result['result']['isError'])
        self.assertEqual(self.fixture.drive.events.count('cas'),1)
        self.assertEqual(self.fixture.drive.manifest['revision'],1)

    async def test_no_queue_group_on_memory_hint_and_targeted_subscriptions(self):
        self.assertTrue(all(not queue for subject,_,queue in self.bus.entries if subject.startswith('xiaozhi.v1.memory.')))
        before=len(self.bus.entries)
        # A replacement connection registers the same targets normally.
        other=Bus();await self.services[0].register(other)
        self.assertEqual({subject for subject,_,_ in other.entries},{wire.subject(NODES[0]),wire.CHANGED})
        self.assertEqual(len(self.bus.entries),before)

    async def test_missed_hint_repaired_by_periodic_refresh(self):
        service=self.services[2]
        refreshes = []
        def refresh():
            service.backend.sync()
            refreshes.append(True)
        service.refresh_blocking=refresh
        service.start()
        for _ in range(100):
            if refreshes: break
            await asyncio.sleep(.001)
        self.assertTrue(refreshes)
        # Publish a new Cloud snapshot directly, without any NATS event.
        scopes = service.backend.scopes()
        scopes[MAC].append(memory_fixtures.entry('new', 'New fact'))
        self.fixture.drive.seed(scopes, revision=2, writer=NODES[0])
        for _ in range(100):
            if service.backend.status()['memory_revision']==2:break
            await asyncio.sleep(.01)
        self.assertEqual(service.backend.status()['memory_revision'],2)
        self.assertGreaterEqual(len(refreshes), 2)

    async def test_policy_mismatch_expiry_and_invalid_arguments(self):
        client=MemoryClient(self.rpc,{**self.binding,'policy':'f'*64},MAC,NODES)
        result=await client.execute({'id':'one','arguments':{'action':'remember','content':'New fact'}},self.history)
        self.assertTrue(result['result']['isError'])
        self.assertEqual(self.fixture.drive.uploads,0)
        value={'protocol':wire.PROTOCOL,'request_id':uuid.uuid4().hex,'core_id':NODES[0],
               'device_id':MAC,'policy':self.digest,'deadline_ms':int(time.time()*1000)-1,
               'action':'remember','arguments':{'content':'New fact'},
               'context':{'active_project':None,'recent_messages':[]}}
        self.assertEqual(wire.reply(self.services[0].execute(value),value['request_id'])['error'],'memory_expired')
        for bad in ({**value,'device_id':'../../credentials'}, {**value,'arguments':{'content':'x','path':'/secret'}},
                    {**value,'arguments':{'content':'x','pinned':'yes'}}, {**value,'context':{'active_project':None,'recent_messages':['x']*7}}):
            with self.assertRaises(ValueError):wire.request(wire.encode(bad,wire.MAX_BYTES))
        with self.assertRaises(ValueError):wire.request(b'x'*(wire.MAX_BYTES+1))

    async def test_recall_disabled_tool_hidden_and_policy_order_stable(self):
        client=MemoryClient(self.rpc,{**self.binding,'tool_enabled':False},MAC,NODES)
        self.assertEqual(client.tools(),[])
        source=dict(reversed(list(self.source.items())))
        self.assertEqual(wire.policy({'selected_module':{'Memory':'x'},'Memory':{'x':source}})[1],self.digest)

    async def http_client(self, index=1):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        from core.api.control_plane_settings import ControlPlaneSettingsHandler
        service = self.services[index]
        reconciliation = service.reconciliation
        reconciliation.memory = service
        reconciliation.config.allow_remote = True
        reconciliation.config.secrets = None
        from core.cluster.config_reconciliation import CONTROL_PROTOCOL
        reconciliation.status = lambda: {'protocol': CONTROL_PROTOCOL, 'node_id': service.node,
                                          'memory': service.status()}
        handler = ControlPlaneSettingsHandler(reconciliation)
        app = web.Application()
        async def cluster(request): return web.json_response(reconciliation.status())
        app.add_routes([
            web.get('/api/cluster', cluster),
            web.get('/api/settings/memory', handler.handle_memory),
            web.post('/api/settings/memory', handler.handle_memory),
            web.put('/api/settings/memory/{entry_id}', handler.handle_memory),
            web.delete('/api/settings/memory/{entry_id}', handler.handle_memory),
            web.get('/api/cluster/memory', handler.handle_memory_cluster),
        ])
        client = TestClient(TestServer(app))
        self.addAsyncCleanup(client.close)
        await client.start_server()
        return client

    async def test_http_crud_from_any_node_and_voice_recall_share_device_scope(self):
        client = await self.http_client(2)
        url = '/api/settings/memory?device_id=' + MAC
        result = await (await client.get('/api/settings/memory')).json()
        self.assertEqual(result['scopes'], [MAC, OTHER])
        self.assertFalse(result['initialized'])
        response = await client.post(url, json={'base_revision': 1, 'content': 'Robot is green'})
        self.assertEqual(response.status, 200, await response.text())
        payload = await response.json()
        self.assertTrue(payload['writable'])
        identifier = next(e['id'] for e in payload['entries'] if e['content'] == 'Robot is green')
        await self.services[1].owned(self.services[1].backend.sync)
        self.assertIn('Robot is green', await self.client.call('list', {}, self.history))
        response = await client.put('/api/settings/memory/' + identifier + '?device_id=' + MAC,
            json={'base_revision': 2, 'content': 'Robot is purple'})
        self.assertEqual(response.status, 200, await response.text())
        response = await client.delete('/api/settings/memory/' + identifier + '?device_id=' + MAC,
            json={'base_revision': 3})
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual((await response.json())['memory_revision'], 4)
        self.assertNotIn('Robot is purple', str(self.services[2].backend.scopes()[MAC]))
        self.assertIn('Other device fact', str(self.services[2].backend.scopes()[OTHER]))
        self.assertEqual(sum(subject == wire.CHANGED for subject, _ in self.bus.published), 3)
        self.assertFalse(self.fixture.path.exists())

    async def test_stale_browser_revision_conflicts_without_rebase_and_refresh_recovers(self):
        client = await self.http_client()
        url = '/api/settings/memory?device_id=' + MAC
        await self.client.call('remember', {'content': 'Voice-saved fact'}, self.history)
        response = await client.post(url, json={'base_revision': 1, 'content': 'Stale edit'})
        self.assertEqual(response.status, 409)
        self.assertEqual((await response.json())['code'], 'memory_conflict')
        self.assertEqual(self.fixture.drive.manifest['revision'], 2)
        response = await client.get(url)
        self.assertEqual((await response.json())['memory_revision'], 2)
        self.assertEqual(self.fixture.drive.events.count('cas'), 1)

    async def test_http_rejects_bad_scope_metadata_revision_and_oversize(self):
        client = await self.http_client()
        url = '/api/settings/memory?device_id=' + MAC
        for body in ({'content': 'Fact'}, {'base_revision': True, 'content': 'Fact'},
                     {'base_revision': 1, 'content': 'Fact', 'pinned': 'yes'},
                     {'base_revision': 1, 'content': 'Fact', 'device_id': OTHER}):
            self.assertEqual((await client.post(url, json=body)).status, 400)
        self.assertEqual((await client.post('/api/settings/memory', json={'base_revision':1,'content':'Fact'})).status,400)
        self.assertEqual((await client.get('/api/settings/memory?device_id=../secrets')).status,400)
        self.assertEqual((await client.post(url,data='x'*17000,headers={'Content-Type':'application/json'})).status,413)
        self.assertEqual(self.fixture.drive.uploads,0)

    async def test_http_cloud_failure_and_nats_hint_failure_do_not_fake_commit_status(self):
        client = await self.http_client()
        url = '/api/settings/memory?device_id=' + MAC
        self.fixture.drive.failure = 'cas'
        response = await client.post(url,json={'base_revision':1,'content':'Conflict'})
        self.assertEqual(response.status,409)
        self.fixture.drive.failure = None
        async def fail(*args, **kwargs): raise ConnectionError('private credentials')
        with patch.object(self.bus, 'publish', side_effect=fail):
            response = await client.post(url,json={'base_revision':1,'content':'Committed fact'})
        self.assertEqual(response.status,200,await response.text())
        self.assertEqual((await response.json())['memory_revision'],2)
        self.assertEqual(self.fixture.drive.manifest['revision'],2)

    async def test_any_node_can_write_when_former_writer_is_offline(self):
        self.bus.entries = [(s,c,q) for s,c,q in self.bus.entries if s != wire.subject(NODES[0])]
        for i in (1,2):
            client = MemoryClient(SimpleNamespace(client=self.bus,core_id=NODES[i]),self.binding,MAC,NODES)
            await client.call('remember',{'content':f'Fact from node {i}'},self.history)
            await client.call('forget',{'content':f'Fact from node {i}'},self.history)
        self.assertEqual(self.fixture.drive.manifest['revision'],5)
        self.assertEqual(self.fixture.drive.events.count('cas'),4)

    async def test_concurrent_cas_loser_keeps_winner_and_never_retries(self):
        def race():
            self.fixture.drive.before_cas = None
            self.services[2].settings(MAC,'POST',{'base_revision':1,'content':'Winning edit'})
        self.fixture.drive.before_cas = race
        result = await self.client.execute({'id':'one','arguments':{'action':'remember','content':'Losing edit'}},self.history)
        self.assertTrue(result['result']['isError'])
        self.assertEqual(self.fixture.drive.manifest['revision'],2)
        self.assertEqual(self.fixture.drive.events.count('cas'),2)
        self.services[1].backend.sync()
        self.assertIn('Winning edit',str(self.services[1].backend.scopes()))
        self.assertNotIn('Losing edit',str(self.services[1].backend.scopes()))

    async def test_shared_manifest_does_not_reenable_legacy_writes_or_downgrade(self):
        from core.memory_storage import MemoryReadOnly, MemoryConflict
        await self.client.call('remember',{'content':'Shared edit'},self.history)
        legacy = CloudMemoryStore(self.fixture.bootstrap,self.fixture.drive,
            self.fixture.directory/'legacy',self.source,materialize=False)
        legacy.sync()
        with self.assertRaises(MemoryReadOnly):legacy.mutate(MAC,2,lambda entries:True)
        self.fixture.drive.seed(self.services[1].backend.scopes(),revision=3,writer=NODES[0])
        with self.assertRaises(MemoryConflict):self.services[1].backend.sync()

    async def test_cluster_status_exposes_only_revision_readiness_and_node_ids(self):
        client = await self.http_client()
        response = await client.get('/api/cluster/memory')
        payload = await response.json()
        self.assertEqual(payload['ready_nodes'],1)
        self.assertEqual(payload['memory_revision'],1)
        text=json.dumps(payload)
        for private in ('Camera uses shared I2C', MAC, 'memory-manifest', str(self.fixture.path)):
            self.assertNotIn(private,text)

    async def test_memory_tool_update_delete_preserves_metadata_and_has_no_other_device_access(self):
        updated = await self.client.execute({'id':'one','arguments':{'action':'update',
            'entry_id':'first','content':'Updated camera fact'}},self.history)
        self.assertFalse(updated['result']['isError'])
        entry = self.services[1].backend.scopes()[MAC][0]
        self.assertTrue(entry['pinned'])
        self.assertEqual(entry['content'],'Updated camera fact')
        forbidden = await self.client.execute({'id':'two','arguments':{'action':'delete','entry_id':'other'}},self.history)
        self.assertTrue(forbidden['result']['isError'])
        deleted = await self.client.execute({'id':'three','arguments':{'action':'delete','entry_id':'first'}},self.history)
        self.assertFalse(deleted['result']['isError'])
        self.assertEqual(self.services[1].backend.scopes()[MAC],[])
        self.assertEqual(self.fixture.drive.manifest['revision'],3)
        tool = self.client.tools()[0]['function']['parameters']['properties']
        self.assertIn('update',tool['action']['enum'])
        self.assertIn('delete',tool['action']['enum'])

    async def test_cluster_sync_status_tracks_all_nodes_and_missed_hint_repair(self):
        clients = [await self.http_client(index) for index in range(3)]
        peers = SimpleNamespace(nodes=tuple((NODES[index],str(client.make_url('/')).rstrip('/'))
                                           for index,client in enumerate(clients)))
        for service in self.services:service.reconciliation.config.secrets=peers
        payload=await (await clients[1].get('/api/cluster/memory')).json()
        self.assertEqual(payload['ready_nodes'],3)
        # Drop the event. The authoritative save must still succeed.
        async def fail(*args,**kwargs):raise ConnectionError
        with patch.object(self.bus,'publish',side_effect=fail):
            response=await clients[2].post('/api/settings/memory?device_id='+MAC,
                json={'base_revision':1,'content':'Cluster fact'})
        self.assertEqual(response.status,200)
        payload=await (await clients[1].get('/api/cluster/memory')).json()
        self.assertEqual(payload['ready_nodes'],1)
        self.assertEqual(payload['memory_revision'],2)
        for service in self.services:await service.owned(service.backend.sync)
        payload=await (await clients[1].get('/api/cluster/memory')).json()
        self.assertEqual(payload['ready_nodes'],3)
        self.assertEqual(payload['state'],'ready')
        # Matching revisions from different authorities must not claim 3/3 sync.
        with patch.object(self.services[0].backend,'manifest_id','other-authority'):
            payload=await (await clients[1].get('/api/cluster/memory')).json()
        self.assertEqual(payload['ready_nodes'],2)
        self.assertFalse(payload['nodes'][0]['authority_matches'])

    async def test_settings_mutation_access_guard_and_strict_json(self):
        client=await self.http_client()
        url='/api/settings/memory?device_id='+MAC
        self.assertEqual((await client.post(url,data='plain text')).status,415)
        self.assertEqual((await client.post(url,data='{"base_revision":1,"base_revision":2,"content":"Fact"}',
            headers={'Content-Type':'application/json'})).status,400)
        service=self.services[1]
        service.nodes.remove(service.node)
        self.assertEqual((await client.post(url,json={'base_revision':1,'content':'Fact'})).status,503)
        self.assertEqual(self.fixture.drive.uploads,0)

    async def test_llm_memory_tool_executes_through_conversation_and_resumes(self):
        from core.cluster.llm_worker import LLMWorker
        from core.cluster.worker_rpc import WorkerRPC
        from core.cluster.nats_config import NatsConfig, NatsConnectionConfig
        from core.cluster import tool_stream_protocol as tooling
        from plugins_func.tool_schemas import MANAGE_MEMORY_FUNCTION_DESC
        bus = self.bus
        bus.is_closed = False
        async def close(): bus.is_closed = True
        bus.close = bus.drain = close
        outputs=[]
        class Provider:
            async def response_tools_async(inner,messages,tools):
                self.assertIn("manage_memory", [tool["function"]["name"] for tool in tools])
                for index, args in enumerate(({'action':'remember','content':'Robot is yellow'},
                                              {'action':'recall','content':'yellow'},
                                              {'action':'forget','content':'yellow'})):
                    result = yield {'calls':[{'id':str(index),'name':'manage_memory','arguments':args}]}
                    outputs.extend(result)
                yield 'Memory actions confirmed'
        cfg=NatsConfig(('nats://fixture:4222',),'fixture','fixture',NODES[2])
        worker=LLMWorker(cfg,asyncio.Event(),{'revision':10,'prompt':'Configured prompt'},Provider())
        worker.client=bus
        await bus.subscribe(tooling.SUBJECT,cb=worker._receive_tools,queue=tooling.QUEUE_GROUP)
        rpc=WorkerRPC(NatsConnectionConfig(cfg.servers,cfg.username,cfg.password),NODES[1],client_factory=lambda:bus)
        async def send(value):pass
        async def chunk(text,seq):pass
        voice=VoiceTurn(rpc,10,AUDIO,'session',send)
        voice.memory=self.client
        try:
            tools=await voice.tools()
            result=await rpc.generate_stream(10,[{'role':'user','content':'Remember then recall and forget'}],
                chunk,tools=tools,on_tools=voice.execute_tools)
            self.assertEqual(result['text'],'Memory actions confirmed')
            self.assertTrue(all(not item['result']['isError'] for item in outputs))
            self.assertIn('Robot is yellow',outputs[1]['result']['content'][0]['text'])
            self.assertEqual(self.fixture.drive.manifest['revision'],3)
        finally:
            await rpc.close();await worker._shutdown()


class HistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_turn_history_survives_worker_selection(self):
        generated=[]
        async def generate(revision,dialogue,on_chunk,**options):
            generated.append(copy.deepcopy(dialogue))
            return {'status':'ok','text':'answer','worker_id':NODES[len(generated)%3]}
        async def send(value):pass
        rpc=SimpleNamespace(core_id=NODES[0],generate_stream=generate)
        voice=VoiceTurn(rpc,10,AUDIO,'session',send)
        await voice.start_text('My name is Sam');await voice.task
        await voice.start_text('What is my name?');await voice.task
        self.assertEqual(generated[1],[{'role':'user','content':'My name is Sam'},
            {'role':'assistant','content':'answer'},{'role':'user','content':'What is my name?'}])
        other=VoiceTurn(rpc,10,AUDIO,'other',send)
        self.assertEqual(other.history.messages,[])

    async def test_failed_and_aborted_turns_are_not_committed(self):
        gate=asyncio.Event()
        async def generate(*args,**kwargs):
            await gate.wait()
            return {'status':'error','error':'llm_provider_failed'}
        async def send(value):pass
        voice=VoiceTurn(SimpleNamespace(core_id=NODES[0],generate_stream=generate),10,AUDIO,'session',send)
        await voice.start_text('Interrupted');await asyncio.sleep(0);await voice.abort()
        self.assertEqual(voice.history.messages,[])
        gate.set();await voice.start_text('Failed');await voice.task
        self.assertEqual(voice.history.messages,[])

    async def test_tool_stream_injects_memory_once_and_keeps_legacy_requests_valid(self):
        from core.cluster import tool_stream_protocol as tooling
        from core.cluster.llm_worker import LLMWorker
        from core.cluster.worker_rpc import WorkerRPC
        from core.cluster.nats_config import NatsConfig, NatsConnectionConfig
        bus = Bus()
        bus.is_closed = False
        async def close(): bus.is_closed = True
        bus.close = bus.drain = close
        seen = []
        class Provider:
            async def response_tools_async(self, messages, tools):
                seen.append(copy.deepcopy(messages))
                yield 'answer'
        cfg = NatsConfig(('nats://fixture:4222',), 'fixture', 'fixture', NODES[1])
        worker = LLMWorker(cfg, asyncio.Event(), {'revision':10, 'prompt':'Before <memory>old</memory> after'}, Provider())
        worker.client = bus
        await bus.subscribe(tooling.SUBJECT, cb=worker._receive_tools, queue=tooling.QUEUE_GROUP)
        rpc = WorkerRPC(NatsConnectionConfig(cfg.servers,cfg.username,cfg.password), NODES[0], client_factory=lambda:bus)
        async def chunk(text, seq): pass
        async def execute(calls): return []
        try:
            kwargs = dict(tools=[], on_tools=execute)
            dialogue = [{'role':'user','content':'question'}]
            result = await rpc.generate_stream(10, dialogue, chunk, memory_context='Saved blue robot', **kwargs)
            self.assertEqual(result['text'],'answer')
            self.assertEqual(seen[0][0]['content'],'Before <memory>\nSaved blue robot\n</memory> after')
            await rpc.generate_stream(10, dialogue, chunk, **kwargs)
            self.assertEqual(seen[1][0]['content'],'Before <memory>old</memory> after')
        finally:
            await rpc.close()
            await worker._shutdown()

    async def test_history_bounds_complete_pairs_and_utf8(self):
        from core.cluster.llm_protocol import dialogue
        history=SessionHistory()
        for _ in range(30):history.complete('question','é'*20000)
        messages=history.dialogue('next question')
        dialogue(messages)
        self.assertLessEqual(len(messages),15)
        self.assertLessEqual(len(wire.encode(history.messages,100000)),24000)
        self.assertEqual(messages[0]['role'],'user')
        self.assertEqual(messages[-2]['role'],'assistant')

    async def test_control_plane_memory_import_does_not_load_provider_or_logger(self):
        script="from core.cluster.memory_service import MemoryService; import sys; assert 'config.logger' not in sys.modules; assert not any(m.startswith('core.providers.') for m in sys.modules)"
        result=subprocess.run([sys.executable,'-c',script],capture_output=True,text=True,env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
        self.assertEqual(result.returncode,0,result.stderr)
