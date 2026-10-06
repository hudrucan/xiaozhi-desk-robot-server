"""Three disposable stores, fake Cloud/NATS, encrypted peer fixtures and loopback UI."""
import asyncio
import copy
import json
import os
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from config.cloud_layers import initial_cluster_object, resolve_layers
from config.cloud_secrets import LocalSecretStore, REFERENCE
from config.config_loader import merge_configs
from config.config_store import ConfigConflict
from config.google_drive_config import GoogleDriveConfigStore
from core.api.control_plane_settings import ControlPlaneSettingsHandler
from core.cluster.config_reconciliation import ConfigReconciliation
from core.cluster.secret_provisioning import SecretProvisioning, ProvisionIncomplete
from core.cluster.secret_transport import SecretProvisionConfig, SecretCipher, canonical
from core.control_plane import create_app
import test_cloud_config as fixtures
from test_control_plane import FakeClient, ENV
from config.control_plane import ControlPlaneConfig

NODES = (('node-a','http://127.0.0.1:8004'),('node-b','http://127.0.0.2:8004'),('node-c','http://127.0.0.3:8004'))
KEY = bytes(range(32))
VALUE = 'fixture-new-gemini-key'


class CipherTests(unittest.TestCase):
    def setUp(self):
        self.config = SecretProvisionConfig(KEY,NODES)
        self.cipher = SecretCipher(self.config)
    def test_authenticated_ciphertext_and_ack_are_bound_to_direction_nodes_and_operation(self):
        message=self.cipher.seal('store','node-a','node-b','a'*32,{'name':'VALUE_'+'b'*32,'value':VALUE})
        data=canonical(message)
        self.assertNotIn(VALUE.encode(),data)
        self.assertNotIn(b'VALUE_',data)
        _,_,payload=self.cipher.open(data,'store','node-b',remote='127.0.0.1')
        self.assertEqual(payload['value'],VALUE)
        for kwargs in ({'target':'node-c'},{'remote':'127.0.0.3'},{'operation':'c'*32},{'action':'stored'}):
            options={'action':'store','target':'node-b',**kwargs}
            with self.assertRaises(Exception):self.cipher.open(data,**options)
        for field,value in (('source','node-c'),('operation','d'*32),('issued_at',int(time.time())-120)):
            corrupt={**message,field:value}
            with self.assertRaises(Exception):self.cipher.open(canonical(corrupt),'store','node-b')
        wrong=SecretCipher(SecretProvisionConfig(bytes(reversed(KEY)),NODES))
        with self.assertRaises(Exception):wrong.open(data,'store','node-b')
        self.assertNotIn(repr(KEY),repr(self.config))

    def test_env_requires_complete_private_three_node_membership(self):
        with patch.dict(os.environ,{},clear=True):self.assertIsNone(SecretProvisionConfig.from_env(8004))
        env={'XIAOZHI_SECRET_PROVISION_KEY':KEY.hex(),'XIAOZHI_SECRET_PROVISION_NODES':json.dumps({f'node-{i}':f'http://10.10.10.{10+i}:8004' for i in (1,2,3)})}
        with patch.dict(os.environ,env,clear=True):self.assertEqual(len(SecretProvisionConfig.from_env(8004).nodes),3)
        for peers in ({'one':'http://10.10.10.11:8004'},dict(NODES),{'node-a':'http://user:password@10.10.10.11:8004','node-b':'http://10.10.10.12:8004','node-c':'http://10.10.10.13:8004'}):
            with patch.dict(os.environ,{**env,'XIAOZHI_SECRET_PROVISION_NODES':json.dumps(peers)},clear=True):
                with self.assertRaises(ValueError):SecretProvisionConfig.from_env(8004)


class ProvisionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture=fixtures.CloudConfigTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        obj=initial_cluster_object(copy.deepcopy(self.fixture.cloud_overrides),'node-a')
        obj['layers']['nodes']={node:{'environment':None,'role':None,'overrides':{}} for node,_ in NODES}
        self.fixture.drive.publish_object(obj,1)
        self.members={};self.reconciliations={};self.stores={};self.nats={};self.offline=set();self.tamper=False
        self.secrets_config=SecretProvisionConfig(KEY,NODES)
        with patch.dict(os.environ,ENV,clear=True):base=ControlPlaneConfig.from_env()
        for node,url in NODES:
            bootstrap={**self.fixture.bootstrap,'node_id':node}
            secrets=LocalSecretStore(node,self.fixture.directory/node/'secrets')
            secrets.put_many(self.fixture.secrets.export_dataset()['values'])
            store=GoogleDriveConfigStore(bootstrap,transport=self.fixture.drive,
                cache_dir=self.fixture.directory/node/'cache',default_path=str(self.fixture.default_path),secret_provider=secrets)
            config=replace(base,host=url.split('//')[1].split(':')[0],secrets=self.secrets_config)
            nats=FakeClient();service=ConfigReconciliation(store,config,client_factory=lambda nats=nats:nats)
            await service.start();self.addAsyncCleanup(service.stop)
            member=SecretProvisioning(service,self.secrets_config,exchange=self.exchange)
            self.addAsyncCleanup(member.stop)
            self.members[node]=member;self.reconciliations[node]=service;self.stores[node]=store;self.nats[node]=nats
        self.service=self.members['node-a']

    async def exchange(self,node,envelope):
        if node in self.offline:raise OSError('private-exception-with-'+VALUE)
        peer=self.members[node]
        source,operation,payload=peer.cipher.open(canonical(envelope),envelope['action'],node,remote=self.secrets_config.address(envelope['source']))
        result=await peer.reconciliation.operation(peer.local,envelope['action'],payload)
        ack=peer.cipher.seal('stored' if envelope['action']=='store' else 'checked',node,source,operation,result)
        if self.tamper:ack['operation']='f'*32
        return canonical(ack)

    def reference(self):
        obj=json.loads(self.fixture.drive.files[self.fixture.drive.manifest['config']['file_id']])
        value=obj['layers']['nodes']['node-a']['overrides'].get('LLM',{}).get('Test',{}).get('api_key') or obj['layers']['cluster']['LLM']['Test']['api_key']
        return REFERENCE.fullmatch(value)[1]

    async def save(self,service=None,revision=1,value=VALUE):
        return await (service or self.service).provision('LLM','Test','api_key',value,revision)

    async def test_three_durable_acknowledgements_before_cas_and_no_plaintext_cloud_nats_or_public_status(self):
        before=copy.deepcopy(self.fixture.drive.manifest)
        def require_all_before_cas():
            obj=json.loads(self.fixture.drive.files[f'upload-{self.fixture.drive.uploads}'])
            name=REFERENCE.fullmatch(obj['layers']['nodes']['node-a']['overrides']['LLM']['Test']['api_key'])[1]
            for store in self.stores.values():self.assertEqual(store.secrets.get(name),VALUE)
        self.fixture.drive.before_commit=require_all_before_cas
        with patch.object(self.stores['node-a'].soundbank_assets,'publish_layers',side_effect=AssertionError('Unexpected local Soundbank publication')):
            result=await self.save()
        self.assertTrue(result['committed']);self.assertEqual(result['revision'],2)
        self.assertEqual(len(result['nodes']),3)
        self.assertEqual(self.fixture.drive.manifest['revision'],before['revision']+1)
        name=self.reference()
        for store in self.stores.values():
            self.assertEqual(store.secrets.get(name),VALUE)
            self.assertIsNone(store.active_revision)
        await asyncio.sleep(.02)
        messages=self.nats['node-a'].messages
        self.assertEqual(len(messages),1)
        self.assertEqual(json.loads(messages[0][1]),{'protocol':'xiaozhi-config-v1','revision':2})
        status=await self.service.status('LLM','Test','api_key')
        self.assertTrue(status['ready']);self.assertTrue(status['consistent'])
        self.assertNotIn(name,json.dumps(status));self.assertNotIn(VALUE,json.dumps(status))
        for content in self.fixture.drive.files.values():self.assertNotIn(VALUE.encode(),content)

    async def test_partial_node_failure_keeps_old_cloud_reference_and_all_active_values(self):
        before=copy.deepcopy(self.fixture.drive.manifest)
        existing={node:store.secrets.export_dataset()['values'] for node,store in self.stores.items()}
        self.offline.add('node-c')
        with self.assertRaises(ProvisionIncomplete) as error:await self.save()
        self.assertEqual(error.exception.nodes[-1],{'node_id':'node-c','state':'unconfirmed'})
        self.assertEqual(self.fixture.drive.manifest,before)
        for node,store in self.stores.items():
            after=store.secrets.export_dataset()['values']
            for name,value in existing[node].items():self.assertEqual(after[name],value)
        self.assertNotIn(VALUE,str(error.exception))
        self.assertEqual(self.nats['node-a'].messages,[])

    async def test_extra_inactive_cloud_assignment_and_shared_layers_are_preserved_exactly(self):
        obj=json.loads(self.fixture.drive.files[self.fixture.drive.manifest['config']['file_id']])
        obj['layers']['nodes']['legacy-node']={'environment':None,'role':None,'overrides':{'prompt':'Legacy prompt'}}
        self.fixture.drive.publish_object(obj,2)
        original=copy.deepcopy(obj)
        await self.save(revision=2)
        updated=json.loads(self.fixture.drive.files[self.fixture.drive.manifest['config']['file_id']])
        self.assertEqual(updated['layers']['nodes']['legacy-node'],original['layers']['nodes']['legacy-node'])
        for layer in ('global','environments','roles','cluster'):
            self.assertEqual(updated['layers'][layer],original['layers'][layer])
        for node,_ in NODES:
            self.assertEqual(updated['layers']['nodes'][node]['overrides']['LLM']['Test']['api_key'],'${secret:'+self.reference()+'}')

    async def test_forged_ack_does_not_publish_cloud(self):
        self.tamper=True
        with self.assertRaises(ProvisionIncomplete):await self.save()
        self.assertEqual(self.fixture.drive.manifest['revision'],1)

    async def test_peer_with_different_cloud_authority_does_not_acknowledge_or_publish(self):
        self.members['node-c'].authority='f'*64
        with self.assertRaises(ProvisionIncomplete):await self.save()
        self.assertEqual(self.fixture.drive.manifest['revision'],1)

    async def test_conflicting_cloud_manifest_keeps_reference_and_all_secret_names_immutable(self):
        original=json.loads(self.fixture.drive.files[self.fixture.drive.manifest['config']['file_id']])['layers']['cluster']['LLM']['Test']['api_key']
        def race():
            obj=json.loads(self.fixture.drive.files[self.fixture.drive.manifest['config']['file_id']])
            obj['layers']['cluster']['prompt']='Concurrent edit'
            self.fixture.drive.publish_object(obj,2)
        self.fixture.drive.before_commit=race
        with self.assertRaises(ConfigConflict):await self.save()
        obj=json.loads(self.fixture.drive.files[self.fixture.drive.manifest['config']['file_id']])
        self.assertEqual(obj['layers']['cluster']['LLM']['Test']['api_key'],original)
        self.assertEqual(obj['layers']['cluster']['prompt'],'Concurrent edit')

    async def test_stale_base_stops_and_explicit_key_rotation_preserves_other_node_overrides(self):
        count=len(self.stores['node-a'].secrets.export_dataset()['values'])
        with self.assertRaises(ConfigConflict):await self.save(revision=9)
        obj=json.loads(self.fixture.drive.files[self.fixture.drive.manifest['config']['file_id']])
        obj['layers']['nodes']['node-c']['overrides']={'LLM':{'Test':{'api_key':'${secret:NODE_OVERRIDE}','temperature':0.3}},'prompt':'Node-specific prompt'}
        self.fixture.drive.publish_object(obj,2)
        await self.save(revision=2)
        self.assertEqual(len(self.stores['node-a'].secrets.export_dataset()['values']),count+1)
        obj=json.loads(self.fixture.drive.files[self.fixture.drive.manifest['config']['file_id']])
        overrides=obj['layers']['nodes']['node-c']['overrides']
        self.assertEqual(overrides['prompt'],'Node-specific prompt')
        self.assertEqual(overrides['LLM']['Test']['temperature'],0.3)
        self.assertEqual(overrides['LLM']['Test']['api_key'],'${secret:'+self.reference()+'}')
        self.assertEqual(self.fixture.drive.manifest['revision'],3)

    async def test_missing_key_status_and_symmetric_save_after_serving_node_changes(self):
        # The currently configured reference is present initially on all nodes.
        result=await self.save()
        for service in self.reconciliations.values():await service.reconcile()
        other=self.members['node-b']
        self.assertTrue((await other.status('LLM','Test','api_key'))['ready'])
        second=await self.save(service=other,revision=2,value='fixture-rotated-key')
        self.assertTrue(second['committed']);self.assertEqual(second['revision'],3)
        for store in self.stores.values():self.assertEqual(store.secrets.get(self.reference()),'fixture-rotated-key')
        self.offline.add('node-c')
        status=await other.status('LLM','Test','api_key')
        self.assertFalse(status['ready']);self.assertEqual(status['nodes'][-1]['state'],'unconfirmed')

    async def test_missing_local_reference_is_reported_without_secret_names(self):
        name=self.reference()
        secret_store=self.stores['node-c'].secrets
        value=secret_store.export_dataset()
        value['values'].pop(name)
        secret_store.path.write_text(json.dumps(value))
        status=await self.service.status('LLM','Test','api_key')
        self.assertFalse(status['ready'])
        self.assertEqual(status['nodes'][-1],{'node_id':'node-c','state':'missing'})
        self.assertNotIn(name,json.dumps(status))

    async def test_ui_api_is_masked_and_csrf_rejected_and_peer_endpoint_requires_ciphertext(self):
        config=self.reconciliations['node-a'].config
        client=TestClient(TestServer(create_app(self.stores['node-a'],config,client_factory=FakeClient,secret_exchange=self.exchange)))
        await client.start_server();self.addAsyncCleanup(client.close)
        body={'group':'LLM','provider':'Test','field':'api_key','value':VALUE,'base_revision':1}
        headers={'X-Xiaozhi-Settings':'1'}
        for forbidden in ({},{**headers,'Origin':'http://untrusted.example'},{**headers,'Sec-Fetch-Site':'cross-site'}):
            response=await client.post('/api/settings/secrets',json=body,headers=forbidden)
            self.assertEqual(response.status,403)
        self.assertEqual(self.fixture.drive.manifest['revision'],1)
        self.assertEqual((await client.post('/internal/settings/secret',json={'value':VALUE})).status,403)
        response=await client.post('/api/settings/secrets',json=body,headers=headers)
        self.assertEqual(response.status,200)
        text=await response.text();self.assertNotIn(VALUE,text);self.assertNotIn(self.reference(),text)
        value=json.loads(text);self.assertTrue(value['committed'])
        self.assertEqual(value['settings']['config']['LLM']['Test']['api_key'],'')
        self.assertTrue(value['settings']['control_plane']['capabilities']['secret_provisioning'])
        status=await (await client.get('/api/settings/secrets?group=LLM&provider=Test&field=api_key')).json()
        self.assertTrue(status['ready'])
        asset=await client.get('/settings/secrets.js');self.assertEqual(asset.status,200)

    async def test_successful_cloud_save_survives_failed_nats_hint(self):
        self.nats['node-a'].fail_publish=True
        result=await self.save();self.assertTrue(result['committed'])
        self.assertEqual(self.fixture.drive.manifest['revision'],2)

    async def test_private_http_peer_stores_and_acknowledges_only_authenticated_ciphertext(self):
        service=self.reconciliations['node-b']
        client=TestClient(TestServer(create_app(self.stores['node-b'],service.config,client_factory=FakeClient)))
        await client.start_server();self.addAsyncCleanup(client.close)
        name='VALUE_'+'a'*32
        envelope=self.service.cipher.seal('store','node-a','node-b','b'*32,{'authority':self.service.authority,'name':name,'value':VALUE})
        for _ in range(2):
            response=await client.post('/internal/settings/secret',json=envelope)
            self.assertEqual(response.status,200)
            content=await response.read()
            self.assertNotIn(VALUE.encode(),content);self.assertNotIn(name.encode(),content)
            _,_,ack=self.service.cipher.open(content,'stored','node-a',source='node-b',operation='b'*32)
            self.assertEqual(ack['status'],'stored')
        self.assertEqual(self.stores['node-b'].secrets.get(name),VALUE)
        corrupt={**envelope,'source':'node-c'}
        self.assertEqual((await client.post('/internal/settings/secret',json=corrupt)).status,403)
        self.assertEqual(self.fixture.drive.manifest['revision'],1)

    async def test_committed_save_stays_successful_if_post_commit_public_refresh_fails(self):
        service=self.reconciliations['node-a']
        handler=ControlPlaneSettingsHandler(service,self.service)
        from aiohttp import web
        app=web.Application();app.router.add_post('/api/settings/secrets',handler.handle_secret_put)
        client=TestClient(TestServer(app));await client.start_server();self.addAsyncCleanup(client.close)
        with patch.object(handler.editor,'read_public',side_effect=OSError('private-path '+VALUE)):
            response=await client.post('/api/settings/secrets',json={'group':'LLM','provider':'Test','field':'api_key','value':VALUE,'base_revision':1},headers={'X-Xiaozhi-Settings':'1'})
        self.assertEqual(response.status,200)
        value=await response.json()
        self.assertTrue(value['committed']);self.assertTrue(value['reload_required'])
        self.assertEqual(self.fixture.drive.manifest['revision'],2)
        self.assertNotIn(VALUE,json.dumps(value))

    async def test_fragmented_encrypted_http_acknowledgement_is_read_completely(self):
        from aiohttp import web
        async def reply(request):
            envelope=json.loads(await request.read())
            data=canonical(self.service.cipher.seal('checked','node-b','node-a',envelope['operation'],{'status':'missing'}))
            response=web.StreamResponse(headers={'Content-Type':'application/json'})
            await response.prepare(request)
            await response.write(data[:20])
            await asyncio.sleep(.01)
            await response.write(data[20:])
            await response.write_eof()
            return response
        app=web.Application();app.router.add_post('/internal/settings/secret',reply)
        server=TestServer(app);await server.start_server();self.addAsyncCleanup(server.close)
        nodes=tuple((node,str(server.make_url('')).rstrip('/') if node=='node-b' else endpoint) for node,endpoint in NODES)
        self.service.config=SecretProvisionConfig(KEY,nodes)
        self.service.exchange=None
        result=await self.service._peer('node-b','status',{'name':self.reference()},'e'*32)
        self.assertEqual(result,{'status':'missing'})

    async def test_secret_values_and_invalid_requests_never_enter_normal_settings_or_peer_urls(self):
        for args in (('server','Test','api_key',VALUE,1),('LLM','Test','password',VALUE,1),('LLM','Test','api_key','has whitespace',1),('LLM','Test','api_key',VALUE,True)):
            with self.assertRaises((ValueError,TypeError)):await self.service.provision(*args)
        self.assertEqual(self.fixture.drive.manifest['revision'],1)
        with self.assertRaises(ValueError):self.service.local('store',{'name':'CURRENT_NAME','value':VALUE})
        with self.assertRaises(ValueError):self.service.local('status',{'name':'../escape'})


if __name__=='__main__':unittest.main()
