"""Firmware-compatible MQTT bootstrap, fake Cloud/NATS and loopback HTTP only."""
import base64
import hashlib
import hmac
import json
import os
import subprocess
import unittest
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch

import yaml
from aiohttp.test_utils import TestClient, TestServer
from core.cluster.mqtt_bootstrap import MqttBootstrapConfig
from core.control_plane import create_app
from test_control_plane import make_fixture, FakeClient

KEY = 'fixture-signing-key'
HEADERS = {'Device-Id':'02:11:22:33:44:55','Client-Id':'fixture-client-uuid'}


class BootstrapTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture,self.store,config=make_fixture();self.addCleanup(self.fixture.doCleanups)
        self.fixture.defaults['server']['timezone_offset']=7
        self.fixture.default_path.write_text(yaml.safe_dump(self.fixture.defaults))
        self.state=self.fixture.directory/'ingress.json';self.state.write_text(json.dumps({'vip':'192.168.1.186'}))
        self.bootstrap=MqttBootstrapConfig(KEY,state_file=str(self.state))
        config=replace(config,bootstrap=self.bootstrap)
        self.nats=FakeClient()
        self.client=TestClient(TestServer(create_app(self.store,config,client_factory=lambda:self.nats)))
        self.addAsyncCleanup(self.client.close)
        await self.client.start_server()

    async def test_post_matches_firmware_and_gateway_hmac_contract_without_provider_runtime(self):
        response=await self.client.post('/xiaozhi/ota/',json={'application':{'version':'1.2.3'},'board':{'type':'esp32-s3-n16r8-cam'}},headers=HEADERS)
        self.assertEqual(response.status,200);value=await response.json()
        self.assertEqual(set(value),{'server_time','firmware','mqtt'})
        mqtt=value['mqtt']
        self.assertEqual(mqtt['endpoint'],'192.168.1.186:1883')
        self.assertEqual(mqtt['client_id'],'GID_desk_robot@@@02_11_22_33_44_55@@@fixture-client-uuid')
        self.assertEqual(json.loads(base64.b64decode(mqtt['username'])),{'ip':'unknown'})
        expected=base64.b64encode(hmac.new(KEY.encode(),(mqtt['client_id']+'|'+mqtt['username']).encode(),hashlib.sha256).digest()).decode()
        self.assertEqual(mqtt['password'],expected)
        self.assertEqual(mqtt['publish_topic'],'device-server')
        self.assertEqual(mqtt['subscribe_topic'],'devices/p2p/02_11_22_33_44_55')
        self.assertEqual(mqtt['keepalive'],240)
        self.assertEqual(value['server_time']['timezone_offset'],420)
        self.assertIs(type(value['server_time']['timestamp']),int)
        self.assertEqual(value['firmware'],{'version':'1.2.3','url':''})
        self.assertNotIn(KEY,json.dumps(value));self.assertNotIn('websocket',value)
        self.assertIn('no-store',response.headers['Cache-Control'])
        self.assertIsNone(self.store.active_revision);self.assertIsNone(self.store.runtime_snapshot)
        self.assertEqual(self.fixture.drive.manifest['revision'],1)
        script="const fs=require('fs');const {validateMqttCredentials}=require('./src/mqtt-auth');const m=JSON.parse(fs.readFileSync(0,'utf8'));const r=validateMqttCredentials(m.client_id,m.username,m.password,'192.0.2.1',{mqttSignatureKey:'fixture-signing-key'});if(r.macAddress!=='02:11:22:33:44:55'||r.uuid!=='fixture-client-uuid')process.exit(1);process.stdout.write('gateway contract passed');"
        gateway=Path(__file__).resolve().parents[4]/'xiaozhi-desk-mqtt-gateway'
        if (gateway/'src/mqtt-auth.js').is_file():
            result=subprocess.run(['node','-e',script],cwd=gateway,input=json.dumps(mqtt),text=True,capture_output=True,check=True)
            self.assertEqual(result.stdout,'gateway contract passed')

    async def test_operator_get_never_returns_credentials_and_device_get_works(self):
        value=await (await self.client.get('/xiaozhi/ota/')).json()
        self.assertEqual(value,{'protocol':'xiaozhi-mqtt-bootstrap-v1','ready':True,'conversation_runtime':False})
        self.assertNotIn('mqtt',value)
        value=await (await self.client.get('/xiaozhi/ota/',headers=HEADERS)).json()
        self.assertEqual(value['mqtt']['endpoint'],'192.168.1.186:1883')
        self.assertEqual(value['firmware']['url'],'')

    async def test_invalid_identity_query_json_and_oversize_body_fail_safely(self):
        for headers in ({},{'Device-Id':'bad','Client-Id':'fixture'},{**HEADERS,'Client-Id':'inject@@@topic'}):
            response=await self.client.post('/xiaozhi/ota/',json={},headers=headers)
            self.assertEqual(response.status,400);self.assertNotIn(KEY,await response.text())
        for body in ('[]','{"a":1,"a":2}','NaN','x'*16385):
            response=await self.client.post('/xiaozhi/ota/',data=body,headers={**HEADERS,'Content-Type':'application/json'})
            self.assertEqual(response.status,400)
        self.assertEqual((await self.client.get('/xiaozhi/ota/?secret=ignored',headers=HEADERS)).status,400)
        self.assertEqual((await self.client.post('/xiaozhi/ota/',data='{}',headers=HEADERS)).status,415)

    async def test_desired_applied_vip_drift_missing_snapshot_and_unhealthy_state_fail_closed(self):
        self.state.write_text(json.dumps({'vip':'192.168.1.187'}))
        self.assertEqual((await self.client.post('/xiaozhi/ota/',json={},headers=HEADERS)).status,503)
        self.state.unlink()
        self.assertEqual((await self.client.get('/xiaozhi/ota/')).status,503)
        self.state.write_text(json.dumps({'vip':'192.168.1.186'}))
        from core.control_plane import RECONCILIATION_KEY
        self.client.server.app[RECONCILIATION_KEY].healthy=False
        self.assertEqual((await self.client.get('/xiaozhi/ota/')).status,503)

    async def test_optional_device_allowlist_is_enforced(self):
        from core.control_plane import RECONCILIATION_KEY
        config=replace(self.client.server.app[RECONCILIATION_KEY].config,
            bootstrap=replace(self.bootstrap,allowed_devices=('02:aa:bb:cc:dd:ee',)))
        client=TestClient(TestServer(create_app(self.store,config,client_factory=FakeClient)))
        await client.start_server();self.addAsyncCleanup(client.close)
        response=await client.post('/xiaozhi/ota/',json={},headers=HEADERS)
        self.assertEqual(response.status,403)
        self.assertEqual((await response.json())['error'],'device_not_allowed')

    async def test_current_entrypoint_imports_no_conversation_provider_or_websocket_server(self):
        import sys
        forbidden=('core.providers','core.connection','core.websocket_server','core.http_server','config.logger')
        self.assertFalse(any(name==prefix or name.startswith(prefix+'.') for name in sys.modules for prefix in forbidden))

    def test_env_is_opt_in_requires_signing_key_and_never_reprs_it(self):
        with patch.dict(os.environ,{},clear=True):self.assertIsNone(MqttBootstrapConfig.from_env())
        env={'XIAOZHI_MQTT_BOOTSTRAP_ENABLED':'true','XIAOZHI_BOOTSTRAP_MQTT_SIGNATURE_KEY':KEY}
        with patch.dict(os.environ,env,clear=True):
            value=MqttBootstrapConfig.from_env();self.assertNotIn(KEY,repr(value))
        for invalid in ({'XIAOZHI_BOOTSTRAP_MQTT_SIGNATURE_KEY':''},{'XIAOZHI_BOOTSTRAP_MQTT_PORT':'0'},{'XIAOZHI_BOOTSTRAP_ALLOWED_DEVICES':'["not-a-mac"]'}):
            with patch.dict(os.environ,{**env,**invalid},clear=True):
                with self.assertRaises(ValueError):MqttBootstrapConfig.from_env()


if __name__=='__main__':unittest.main()
