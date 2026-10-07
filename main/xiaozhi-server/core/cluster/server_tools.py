"""Allowlisted server functions executed locally by the selected LLM worker.

Only declarations and bounded results cross NATS. Plugin options/credentials
belong to the private immutable worker bundle, never request payloads.
"""
import asyncio
import copy
import json
import logging
import time
from collections import OrderedDict
from datetime import datetime

from plugins_func.tool_schemas import (GET_CURRENT_DATETIME_FUNCTION_DESC,
    GET_WEATHER_FUNCTION_DESC, GET_AIR_QUALITY_FUNCTION_DESC, WEB_SEARCH_FUNCTION_DESC)
from . import tool_stream_protocol as wire

SCHEMAS = {item['function']['name']: item for item in (
    GET_CURRENT_DATETIME_FUNCTION_DESC, GET_WEATHER_FUNCTION_DESC,
    GET_AIR_QUALITY_FUNCTION_DESC, WEB_SEARCH_FUNCTION_DESC)}
OPTION_FIELDS = {
    'get_weather': {'provider', 'default_location', 'location_aliases', 'language',
                    'preferred_country_code', 'forecast_days', 'cache_ttl_seconds'},
    'get_air_quality': {'provider', 'default_location', 'language',
                        'preferred_country_code', 'forecast_hours', 'cache_ttl_seconds'},
    'web_search': {'provider', 'api_key', 'max_results', 'search_depth', 'include_answer',
                   'country', 'language'},
    'get_current_datetime': set()}


def validate_config(value):
    if (not isinstance(value, dict) or set(value) != {'functions', 'plugins'}
            or not isinstance(value['functions'], list)
            or any(not isinstance(name, str) or name not in SCHEMAS for name in value['functions'])
            or len(set(value['functions'])) != len(value['functions'])
            or not isinstance(value['plugins'], dict)
            or set(value['plugins']) - set(value['functions'])):
        raise ValueError('Invalid server tool configuration')
    for name, options in value['plugins'].items():
        if not isinstance(options, dict) or set(options) - OPTION_FIELDS[name] - {'description'}:
            raise ValueError('Unsupported server tool options')
        for key, option in options.items():
            if key in {'forecast_days', 'forecast_hours', 'max_results', 'cache_ttl_seconds'}:
                upper = {'forecast_days':16, 'forecast_hours':168, 'max_results':10,
                         'cache_ttl_seconds':86400}[key]
                if type(option) is not int or not 1 <= option <= upper:
                    raise ValueError('Invalid server tool bounds')
            elif key == 'location_aliases':
                if (not isinstance(option, dict) or len(option) > 64
                        or any(not isinstance(k, str) or not isinstance(v, str)
                               or len(k) > 256 or len(v) > 256 for k, v in option.items())):
                    raise ValueError('Invalid tool location aliases')
            elif key == 'include_answer' and type(option) is bool:
                continue
            elif not isinstance(option, str) or len(option) > (8192 if key == 'description' else 4096 if key == 'api_key' else 512):
                raise ValueError('Invalid server tool option')
        key = options.get('api_key', '')
        if '${' in key or any(c in key for c in '\x00\r\n'):
            raise ValueError('Unresolved server tool credential')
    return value


def export_config(config, secrets):
    selected = config.get('selected_module', {}).get('Intent')
    intent = config.get('Intent', {}).get(selected, {})
    enabled = intent.get('functions', []) if intent.get('type') == 'function_call' else []
    names = [name for name in SCHEMAS if name in enabled]
    plugins = {}
    for name in names:
        source = config.get('plugins', {}).get(name, {})
        options = {k:copy.deepcopy(v) for k,v in source.items() if k in OPTION_FIELDS[name] | {'description'}}
        if name in {'get_weather', 'get_air_quality'} and str(options.get('provider', '')).strip().lower() != 'open_meteo':
            continue
        if name == 'web_search' and (str(options.get('provider', '')).strip().lower() not in {'metaso', 'tavily'}
                                     or not options.get('api_key')):
            continue
        if 'provider' in options:
            options['provider'] = options['provider'].strip().lower()
        if name == 'web_search' and options.get('api_key'):
            from config.cloud_secrets import MissingSecret
            try:
                options = secrets.resolve({'plugins': {name: options}})['plugins'][name]
            except MissingSecret:
                # An optional search credential must not disable voice or
                # unrelated tools. Invalid datasets still fail export.
                logging.getLogger(__name__).warning(
                    'Web search unavailable; optional credential is not stored locally')
                continue
        plugins[name] = options
    return validate_config({'functions':list(plugins), 'plugins':plugins})


class ServerTools:
    def __init__(self, config=None):
        self.config = validate_config(config or {'functions':[], 'plugins':{}})
        self.cache = OrderedDict()

    def tools(self):
        tools = [copy.deepcopy(SCHEMAS[name]) for name in self.config['functions']]
        for item in tools:
            description = self.config['plugins'].get(item['function']['name'], {}).get('description')
            if description:
                item['function']['description'] = description
        return tools

    async def execute(self, call):
        name, args = call['name'], call['arguments']
        try:
            if name not in self.config['functions']:
                raise ValueError('Unadvertised server function')
            schema = SCHEMAS[name]['function']['parameters']
            if set(args) - set(schema['properties']) or not set(schema.get('required', [])) <= set(args):
                raise ValueError('Invalid server function arguments')
            for key, value in args.items():
                prop = schema['properties'][key]
                if prop['type'] == 'string':
                    if not isinstance(value, str) or not value.strip() or len(value.encode()) > 2048:
                        raise ValueError('Invalid tool text')
                elif type(value) is not int or not prop['minimum'] <= value <= prop['maximum']:
                    raise ValueError('Invalid tool integer')
            async with asyncio.timeout(wire.TOOL_SECONDS):
                text = await self.invoke(name, args, self.config['plugins'].get(name, {}))
            result = {'isError':False, 'content':[{'type':'text', 'text':text}]}
            wire.rpc.encode(result, 14336)
        except asyncio.CancelledError:
            raise
        except Exception:
            # No provider exceptions, arguments, URLs or credentials in results.
            result = {'isError':True, 'error':'Server tool failed or timed out; execution was not retried'}
        return {'id':call['id'], 'result':result}

    async def invoke(self, name, args, options):
        if name == 'get_current_datetime':
            now = datetime.now().astimezone()
            return json.dumps({'observed_at':now.isoformat(timespec='seconds'),
                'date':now.date().isoformat(), 'weekday':now.strftime('%A'),
                'iso_weekday':now.isoweekday(), 'time':now.strftime('%H:%M:%S'),
                'utc_offset':now.strftime('%z'), 'timezone':str(now.tzinfo)})
        if name == 'web_search':
            from plugins_func.functions.search.http_search import _search_metaso, _search_tavily
            key = options.get('api_key', '')
            if not key:
                return 'Web search is not configured.'
            size = options.get('max_results', 3)
            if options.get('provider') == 'metaso':
                return await _search_metaso(key, args['query'], size)
            if options.get('provider') == 'tavily':
                return await _search_tavily(key, args['query'], size,
                    search_depth=options.get('search_depth', 'advanced'),
                    include_answer=options.get('include_answer', 'advanced'),
                    country=options.get('country', ''), language=options.get('language', ''))
            return 'Web search is not configured.'
        if options.get('provider') != 'open_meteo':
            return 'The requested location tool is not configured.'
        location = args.get('location', options.get('default_location', '')).strip()
        if not location:
            return 'A location is required.'
        aliases = options.get('location_aliases', {})
        location = {k.strip().casefold():v.strip() for k,v in aliases.items()}.get(location.casefold(), location)
        params = {'location':location, 'language':args.get('lang', options.get('language', 'en')),
                  'preferred_country_code':options.get('preferred_country_code', '')}
        window = (options.get('forecast_days', 7) if name == 'get_weather'
                  else args.get('forecast_hours', options.get('forecast_hours', 24)))
        cache_key = json.dumps([name, params, window], sort_keys=True)
        cached = self.cache.get(cache_key)
        if cached is not None and cached[0] > time.monotonic():
            self.cache.move_to_end(cache_key)
            return cached[1]
        if name == 'get_weather':
            from plugins_func.functions.weather.open_meteo import fetch_weather_report
            result = await fetch_weather_report(**params, forecast_days=window)
        else:
            from plugins_func.functions.air_quality.open_meteo import fetch_air_quality_report
            result = await fetch_air_quality_report(**params, forecast_hours=window)
        if isinstance(result, str) and 0 < len(result.encode()) <= 14000:
            self.cache[cache_key] = (time.monotonic() + max(60, options.get('cache_ttl_seconds', 1800)), result)
            self.cache.move_to_end(cache_key)
            while len(self.cache) > 64:
                self.cache.popitem(last=False)
        return result or 'No matching location was found.'
