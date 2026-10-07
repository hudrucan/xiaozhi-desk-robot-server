"""Offline built-in tools, credential boundaries and mixed tool continuation."""
import asyncio
import copy
import json
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.cluster.server_tools import ServerTools, export_config, validate_config
from core.cluster import tool_stream_protocol as wire
import test_cluster_mcp as device_tests
CALL = device_tests.CALL

DATE = {'id':'server-date', 'name':'get_current_datetime', 'arguments':{}}


class ServerFunctionTests(unittest.IsolatedAsyncioTestCase):
    async def test_datetime_and_invalid_arguments_are_bounded(self):
        tools = ServerTools({'functions':['get_current_datetime'], 'plugins':{}})
        result = await tools.execute(DATE)
        data = json.loads(result['result']['content'][0]['text'])
        self.assertIn('utc_offset', data)
        self.assertIn('observed_at', data)
        self.assertTrue((await tools.execute({**DATE, 'arguments':{'secret':'private'}}))['result']['isError'])

    async def test_weather_and_air_reuse_existing_async_http_adapters(self):
        options = {'provider':'open_meteo', 'default_location':'Fixture City',
                   'language':'en', 'preferred_country_code':'US'}
        tools = ServerTools({'functions':['get_weather','get_air_quality'],
                             'plugins':{'get_weather':{**options,'location_aliases':{'home':'Fixture City'}},
                                        'get_air_quality':options}})
        for name, module in [('get_weather','weather'),('get_air_quality','air_quality')]:
            with patch(f'plugins_func.functions.{module}.open_meteo.fetch_{module}_report',
                       new=AsyncMock(return_value='Fixture report')) as fetch:
                result = await tools.execute({'id':'location', 'name':name, 'arguments':{'location':'home'}})
                self.assertFalse(result['result']['isError'])
                self.assertEqual(fetch.await_count, 1)
                if name == 'get_weather':
                    self.assertEqual(fetch.call_args.kwargs['location'], 'Fixture City')

    async def test_location_cache_is_bounded_and_respects_ttl(self):
        tools = ServerTools({'functions':['get_weather'], 'plugins':{'get_weather':{
            'provider':'open_meteo', 'cache_ttl_seconds':60}}})
        call = {'id':'weather', 'name':'get_weather', 'arguments':{'location':'Fixture City'}}
        with patch('plugins_func.functions.weather.open_meteo.fetch_weather_report',
                   new=AsyncMock(return_value='Fixture report')) as fetch:
            await tools.execute(call)
            await tools.execute(call)
            self.assertEqual(fetch.await_count, 1)
            with patch('core.cluster.server_tools.time.monotonic', return_value=10**12):
                await tools.execute(call)
            self.assertEqual(fetch.await_count, 2)
            for index in range(70):
                await tools.execute({**call, 'arguments':{'location':f'Fixture {index}'}})
            self.assertEqual(len(tools.cache), 64)

    async def test_search_credentials_stay_in_local_http_adapter_and_errors_are_safe(self):
        tools = ServerTools({'functions':['web_search'], 'plugins':{'web_search':{
            'provider':'tavily', 'api_key':'private-fixture-key', 'max_results':3}}})
        call = {'id':'search', 'name':'web_search', 'arguments':{'query':'Fixture query'}}
        self.assertNotIn('private-fixture-key', str(tools.tools()))
        with patch('plugins_func.functions.search.http_search._search_tavily',
                   new=AsyncMock(return_value='Fixture results')) as fetch:
            result = await tools.execute(call)
            self.assertEqual(fetch.call_args.args, ('private-fixture-key','Fixture query',3))
            self.assertNotIn('private-fixture-key', str(result))
        with patch('plugins_func.functions.search.http_search._search_tavily',
                   new=AsyncMock(side_effect=RuntimeError('private-fixture-key'))):
            result = await tools.execute(call)
            self.assertTrue(result['result']['isError'])
            self.assertNotIn('private-fixture-key', str(result))

    async def test_oversize_result_and_abort_do_not_retry(self):
        tools = ServerTools({'functions':['get_current_datetime'], 'plugins':{}})
        with patch.object(tools, 'invoke', new=AsyncMock(return_value='x'*15000)) as invoke:
            self.assertTrue((await tools.execute(DATE))['result']['isError'])
            self.assertEqual(invoke.await_count, 1)
        with patch.object(tools, 'invoke', new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await tools.execute(DATE)

    def test_export_uses_configured_intent_and_resolves_only_search_key(self):
        config = {'selected_module':{'Intent':'function_call'},
            'Intent':{'function_call':{'type':'function_call','functions':['get_current_datetime','web_search']}},
            'plugins':{'web_search':{'provider':'tavily','api_key':'${secret:search-key}',
                                    'private_path':'unused'}, 'unrelated':{'api_key':'${secret:unused}'}}}
        resolutions = []
        def resolve(value):
            resolutions.append(copy.deepcopy(value))
            value['plugins']['web_search']['api_key'] = 'private-fixture-key'
            return value
        value = export_config(config, SimpleNamespace(resolve=resolve))
        self.assertEqual(value['functions'], ['get_current_datetime','web_search'])
        self.assertEqual(len(resolutions), 1)
        self.assertNotIn('unused', str(resolutions))
        self.assertNotIn('private_path', str(value))
        with self.assertRaises(ValueError):validate_config({'functions':['arbitrary_module'], 'plugins':{}})
        with self.assertRaises(ValueError):validate_config({'functions':['web_search'], 'plugins':{
            'web_search':{'api_key':'${secret:key}'}}})

    def test_missing_optional_search_secret_omits_search_without_hiding_invalid_dataset(self):
        from config.cloud_secrets import MissingSecret
        config = {'selected_module':{'Intent':'tools'},
            'Intent':{'tools':{'type':'function_call','functions':['get_current_datetime','web_search']}},
            'plugins':{'web_search':{'provider':'tavily','api_key':'${secret:fixture}'}}}
        resolver = SimpleNamespace(resolve=lambda value: (_ for _ in ()).throw(MissingSecret('private fixture')))
        with self.assertLogs('core.cluster.server_tools', level='WARNING') as logs:
            value = export_config(config, resolver)
        self.assertEqual(value['functions'], ['get_current_datetime'])
        self.assertNotIn('private fixture', str(logs.output))
        resolver.resolve = lambda value: (_ for _ in ()).throw(ValueError('Invalid dataset'))
        with self.assertRaises(ValueError):
            export_config(config, resolver)

    def test_shared_tool_modules_have_no_config_or_provider_import_side_effects(self):
        script = "import sys; import core.cluster.server_tools, core.utils.wakeup_match, plugins_func.tool_schemas; assert not any(n.startswith(('config.logger', 'core.providers', 'core.connection', 'google.genai')) for n in sys.modules)"
        result = subprocess.run([sys.executable,'-c',script],capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)


class ServerStreamTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = device_tests.ToolStreamTests.asyncSetUp
    asyncTearDown = device_tests.ToolStreamTests.asyncTearDown
    call = device_tests.ToolStreamTests.call
    execute = device_tests.ToolStreamTests.execute
    chunk = device_tests.ToolStreamTests.chunk

    async def test_server_results_resume_same_model_without_core_execution(self):
        self.worker.server_tools = ServerTools({'functions':['get_current_datetime'], 'plugins':{}})
        self.provider.calls = [DATE]
        await self.call()
        self.assertFalse(self.called)
        self.assertEqual(self.provider.results[0]['id'], DATE['id'])
        self.assertFalse(any(json.loads(data).get('kind') == 'tools' for _,data in self.bus.published))
        self.assertEqual(''.join(self.chunks), 'Status confirmed')

    async def test_mixed_device_and_server_calls_preserve_order(self):
        self.worker.server_tools = ServerTools({'functions':['get_current_datetime'], 'plugins':{}})
        self.provider.calls = [CALL, DATE, {**CALL,'id':'second-device'}]
        await self.call()
        self.assertEqual([r['id'] for r in self.provider.results],
                         ['fixture-call','server-date','second-device'])
        self.assertEqual([c['id'] for c in self.called], ['fixture-call','second-device'])

    async def test_exit_direct_response_skips_model_continuation_and_later_actuators(self):
        from plugins_func.tool_schemas import handle_exit_intent_function_desc
        call = {'id':'exit', 'name':'handle_exit_intent', 'arguments':{}}
        self.provider.calls = [call, CALL]
        async def execute(calls):
            self.called.extend(calls)
            return [{'id':call['id'], 'result':{'isError':False,
                'action':'end_conversation', 'text':'Fixture farewell'}}]
        result = await self.rpc.generate_stream(9,[{'role':'user','content':'Please end this conversation'}],
            self.chunk, tools=[handle_exit_intent_function_desc,
                {'type':'function','function':{'name':CALL['name'],'description':'Status',
                'parameters':{'type':'object','properties':{}}}}], on_tools=execute, seconds=30)
        self.assertEqual(result['text'], 'Fixture farewell')
        self.assertEqual(self.called, [call])
        self.assertIsNone(self.provider.results)
        self.assertEqual(self.provider.closed, 1)
