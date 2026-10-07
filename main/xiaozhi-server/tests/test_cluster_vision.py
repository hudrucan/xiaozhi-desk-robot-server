"""Camera auth, bounded transfer and lifecycle with fake HTTP/NATS/providers."""
import asyncio
import copy
import hashlib
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.cluster import vision_protocol as wire
from core.cluster.vision_config import export_config, validate
from core.cluster.vision_http import upload
from core.cluster.vision_worker import VisionService

KEY, TOKEN = 'a' * 32, 'b' * 32
JPEG = b'\xff\xd8\xff' + b'image-fixture' + b'\xff\xd9'


def admission(**changes):
    return {'protocol':wire.PROTOCOL, 'op':'admit', 'job_id':KEY, 'token':TOKEN,
            'revision':9, 'deadline_ms':int(time.time() * 1000) + 14000,
            'size':len(JPEG), 'sha256':hashlib.sha256(JPEG).hexdigest(),
            'question':'Describe the fixture', **changes}


class Bus:
    is_connected = True
    def __init__(self):
        self.sent, self.subscriptions = [], []
    async def publish(self, subject, data):
        self.sent.append((subject, data))
    async def subscribe(self, subject, **options):
        self.subscriptions.append((subject, options))


class TransferTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bus = Bus()
        self.provider = SimpleNamespace(response=AsyncMock(return_value='Fixture result'), close=AsyncMock())
        self.activity = SimpleNamespace(counts={'llm':0})
        self.activity.change = lambda kind, delta: self.activity.counts.__setitem__(kind, self.activity.counts[kind] + delta)
        self.service = VisionService(self.bus, 'deskb2x', 9, self.provider, self.activity)
    async def asyncTearDown(self):
        await self.service.close()
        self.assertEqual(self.activity.counts['llm'], 0)
    async def receive(self, value):
        data = value if isinstance(value, bytes) else wire.encode(value, 4096)
        await self.service.receive(SimpleNamespace(data=data, reply='_INBOX.fixture'))
        return wire.response(self.bus.sent[-1][1], KEY)
    async def chunks(self, data=JPEG, offset=0, token=TOKEN):
        return await self.receive(KEY.encode() + token.encode() + offset.to_bytes(4, 'big') + data)
    async def finish(self):
        await self.receive({'protocol':wire.PROTOCOL, 'op':'finish', 'job_id':KEY, 'token':TOKEN})
        task = self.service.jobs[KEY]['task']
        await task
        return wire.response(self.bus.sent[-1][1], KEY)
    async def test_queue_admission_then_targeted_upload_runs_one_provider(self):
        await self.service.start()
        self.assertEqual(self.bus.subscriptions[0][1]['queue'], wire.QUEUE)
        self.assertNotIn('queue', self.bus.subscriptions[1][1])
        self.assertEqual((await self.receive(admission()))['status'], 'admitted')
        self.assertEqual((await self.chunks())['offset'], len(JPEG))
        self.assertEqual((await self.finish())['text'], 'Fixture result')
        self.provider.response.assert_awaited_once_with('Describe the fixture', JPEG)
        self.assertFalse(self.service.jobs)
    async def test_revision_mismatch_and_busy_do_not_infer(self):
        self.assertEqual((await self.receive(admission(revision=8)))['error'], 'vision_revision_mismatch')
        await self.receive(admission())
        self.assertEqual((await self.receive(admission()))['error'], 'vision_busy')
        self.provider.response.assert_not_awaited()
    async def test_wrong_token_cannot_upload_or_cancel_another_job(self):
        await self.receive(admission())
        self.assertEqual((await self.chunks(token='c' * 32))['error'], 'vision_unavailable')
        await self.receive({'protocol':wire.PROTOCOL, 'op':'cancel', 'job_id':KEY, 'token':'c' * 32})
        self.assertIn(KEY, self.service.jobs)
        self.assertFalse(self.service.jobs[KEY]['image'])
    async def test_out_of_order_chunk_releases_job_without_inference(self):
        await self.receive(admission())
        self.assertEqual((await self.chunks(offset=1))['error'], 'vision_invalid_request')
        self.assertFalse(self.service.jobs)
        self.provider.response.assert_not_awaited()
    async def test_checksum_failure_releases_job(self):
        await self.receive(admission(sha256='0' * 64))
        await self.chunks()
        result = await self.receive({'protocol':wire.PROTOCOL, 'op':'finish', 'job_id':KEY, 'token':TOKEN})
        self.assertEqual(result['error'], 'vision_invalid_request')
        self.assertFalse(self.service.jobs)
    async def test_stop_cancels_provider_and_restores_capacity(self):
        started = asyncio.Event()
        async def wait(*args):
            started.set()
            await asyncio.Event().wait()
        self.provider.response.side_effect = wait
        await self.receive(admission()); await self.chunks()
        await self.receive({'protocol':wire.PROTOCOL, 'op':'finish', 'job_id':KEY, 'token':TOKEN})
        await started.wait()
        await self.service.stop()
        self.assertFalse(self.service.jobs)
        self.service.stopping = False
        self.assertEqual((await self.receive(admission()))['status'], 'admitted')
    async def test_provider_exception_does_not_leak_message(self):
        self.provider.response.side_effect = RuntimeError('private credential fixture')
        await self.receive(admission()); await self.chunks()
        self.assertEqual((await self.finish())['error'], 'vision_provider_failed')
        self.assertNotIn(b'private credential', self.bus.sent[-1][1])
    async def test_expired_upload_is_reaped(self):
        await self.service.start()
        await self.receive(admission())
        self.service.jobs[KEY]['value']['deadline_ms'] = 0
        await asyncio.sleep(.3)
        self.assertFalse(self.service.jobs)


class Fields:
    def __init__(self, fields):
        self.fields = iter(fields)
    async def next(self):
        return next(self.fields, None)


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_selects_once_and_targets_every_chunk_and_cancel(self):
        from core.cluster.vision_client import explain
        routed = []
        pending = {}
        class FakeNats:
            is_connected = True
            async def publish(self, subject, data, reply=None):
                if reply is None:
                    future = pending.get(subject)
                    if future is not None and not future.done():
                        future.set_result(SimpleNamespace(data=data))
                else:
                    routed.append((subject, len(data)))
                    await service.receive(SimpleNamespace(data=data, reply=reply))
            async def request(self, subject, data, timeout):
                routed.append((subject, len(data)))
                reply = '_INBOX.fixture_' + str(len(routed))
                future = asyncio.get_running_loop().create_future()
                pending[reply] = future
                try:
                    await service.receive(SimpleNamespace(data=data, reply=reply))
                    return await asyncio.wait_for(future, timeout)
                finally:
                    pending.pop(reply, None)
        bus = FakeNats()
        provider = SimpleNamespace(response=AsyncMock(return_value='Fixture result'), close=AsyncMock())
        service = VisionService(bus, 'deskb3x', 9, provider)
        image = b'\xff\xd8\xff' + b'x' * (wire.CHUNK * 2) + b'\xff\xd9'
        try:
            self.assertEqual(await explain(bus, 9, 'Describe the fixture', image), 'Fixture result')
            self.assertEqual(routed[0][0], wire.SUBJECT)
            self.assertTrue(all(subject == wire.target('deskb3x') for subject, _ in routed[1:]))
            self.assertTrue(all(size <= wire.CHUNK + 68 for _, size in routed))
            provider.response.assert_awaited_once_with('Describe the fixture', image)
            self.assertFalse(service.jobs)
        finally:
            await service.close()


class Part:
    def __init__(self, name, content):
        self.name, self.content = name, content
    async def read_chunk(self, size):
        chunk, self.content = self.content[:size], self.content[size:]
        return chunk


class HttpTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        mcp = SimpleNamespace(closed=False, camera_active=True, camera_upload=False, camera_consumed=False,
            vision={'token':'deskb1x.' + KEY + '.' + TOKEN}, device_id='aa:bb:cc:dd:ee:ff',
            client_id='fixture-client', camera_question='Describe the fixture', vision_tasks=set())
        voice = SimpleNamespace(generation=3, record=lambda *args, **kwargs: None)
        core = SimpleNamespace(config=SimpleNamespace(node_id='deskb1x'), stopping=False,
            mcps={KEY:mcp}, voices={KEY:voice}, voice_revision=9,
            worker_rpc=SimpleNamespace(client=SimpleNamespace(is_connected=True), streams=set()))
        request = SimpleNamespace(query_string='', content_type='multipart/form-data', headers={
            'Authorization':'Bearer ' + mcp.vision['token'], 'Device-Id':mcp.device_id, 'Client-Id':mcp.client_id},
            multipart=AsyncMock(return_value=Fields([Part('question', b'Describe the fixture'), Part('file', JPEG)])))
        return request, core, mcp
    async def test_valid_session_camera_upload_returns_existing_firmware_contract(self):
        request, core, mcp = self.fixture()
        with patch('core.cluster.vision_client.explain', new=AsyncMock(return_value='Fixture result')) as explain:
            response = await upload(request, core)
            self.assertEqual(response.status, 200)
            self.assertEqual(wire.decode(response.body, 4096),
                             {'success':True, 'action':'RESPONSE', 'response':'Fixture result'})
            explain.assert_awaited_once()
            self.assertEqual((await upload(request, core)).status, 503)
        self.assertFalse(mcp.vision_tasks)
    async def test_wrong_device_session_or_inactive_camera_is_rejected_before_read(self):
        for kind in ('device', 'session', 'inactive'):
            request, core, mcp = self.fixture()
            if kind == 'device': request.headers['Device-Id'] = '00:00:00:00:00:00'
            if kind == 'session': request.headers['Authorization'] = 'Bearer deskb1x.' + 'c' * 32 + '.' + TOKEN
            if kind == 'inactive': mcp.camera_active = False
            self.assertEqual((await upload(request, core)).status, 401)
            request.multipart.assert_not_awaited()
    async def test_changed_generation_rejects_stale_result(self):
        request, core, mcp = self.fixture()
        async def changed(*args):
            core.voices[KEY].generation += 1
            return 'Stale fixture'
        with patch('core.cluster.vision_client.explain', new=changed):
            self.assertEqual((await upload(request, core)).status, 503)
    async def test_core_nats_disconnect_cancels_upload_and_clears_ownership(self):
        request, core, mcp = self.fixture()
        started = asyncio.Event()
        async def pending(*args):
            started.set()
            await asyncio.Event().wait()
        with patch('core.cluster.vision_client.explain', new=pending):
            task = asyncio.create_task(upload(request, core))
            await started.wait()
            for owner in tuple(core.worker_rpc.streams): owner.fail()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertFalse(core.worker_rpc.streams)
        self.assertFalse(mcp.vision_tasks)


class ConfigTests(unittest.TestCase):
    def test_optional_missing_key_disables_vision_without_llm_credential_reuse(self):
        config = {'selected_module':{'VLLM':'Vision'}, 'VLLM':{'Vision':{
            'type':'gemini', 'model_name':'fixture-vision', 'api_key':'your_api_key'}},
            'LLM':{'Gemini':{'api_key':'private-llm-fixture'}}}
        before = copy.deepcopy(config)
        secrets = SimpleNamespace(resolve=lambda value:value)
        self.assertIsNone(export_config(config, secrets))
        self.assertEqual(config, before)
    def test_no_sdk_import_needed_to_validate_private_vision_options(self):
        value = {'type':'gemini', 'model_name':'fixture-vision', 'api_key':'fixture-only'}
        self.assertEqual(validate(value), value)
        with self.assertRaises(ValueError): validate({**value, 'api_key':'${secret:PRIVATE}'})
    def test_protocol_rejects_oversize_images_questions_and_credential_fields(self):
        for changes in ({'size':wire.MAX_IMAGE + 1}, {'question':'x' * 2049}, {'api_key':'private-fixture'}):
            with self.assertRaises(ValueError): wire.request(wire.encode(admission(**changes), 4096))


if __name__ == '__main__':
    unittest.main()
