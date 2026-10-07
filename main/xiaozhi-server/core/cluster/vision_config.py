"""Private selected VLLM options carried in the existing immutable LLM bundle."""
import copy
import logging
import math

FIELDS = {'type', 'api_key', 'model_name', 'response_language', 'max_output_tokens',
          'thinking_level', 'media_resolution', 'temperature', 'top_p', 'top_k'}


def validate(value):
    if (not isinstance(value, dict) or set(value) - FIELDS
            or value.get('type') != 'gemini'
            or not isinstance(value.get('api_key'), str) or not value['api_key'].strip()
            or len(value['api_key']) > 4096 or '${' in value['api_key']
            or value['api_key'].casefold().startswith(('your_', 'your-', '\u4f60'))
            or any(c in value['api_key'] for c in '\x00\r\n')
            or not isinstance(value.get('model_name'), str) or not 1 <= len(value['model_name']) <= 256):
        raise ValueError('Selected vision adapter or credential is unavailable')
    if type(value.get('max_output_tokens', 256)) is not int or not 1 <= value.get('max_output_tokens', 256) <= 2048:
        raise ValueError('Invalid vision output budget')
    for key, maximum in (('temperature', 2), ('top_p', 1)):
        number = value.get(key)
        if number is not None and (type(number) not in (int, float) or not math.isfinite(number) or not 0 <= number <= maximum):
            raise ValueError('Invalid vision generation option')
    if value.get('top_k') is not None and (type(value['top_k']) is not int or not 1 <= value['top_k'] <= 1024):
        raise ValueError('Invalid vision top_k')
    if value.get('thinking_level') not in (None, '', 'minimal', 'low', 'medium', 'high'):
        raise ValueError('Invalid vision thinking level')
    if value.get('media_resolution', 'medium') not in (None, 'low', 'medium', 'high'):
        raise ValueError('Invalid vision media resolution')
    language = value.get('response_language', 'English')
    if not isinstance(language, str) or not 1 <= len(language) <= 128:
        raise ValueError('Invalid vision language')
    return value


def export_config(config, secrets):
    from config.cloud_secrets import MissingSecret
    selected = config.get('selected_module', {}).get('VLLM')
    if not selected:
        return None
    source = config.get('VLLM', {}).get(selected, {})
    if source.get('type', selected) != 'gemini':
        raise ValueError('Selected vision adapter is not deployed')
    if source.get('http_proxy') or source.get('https_proxy'):
        raise ValueError('Proxy-enabled vision requires a reviewed adapter')
    value = copy.deepcopy({key: item for key, item in source.items() if key in FIELDS})
    value['type'] = 'gemini'
    try:
        value = secrets.resolve({'VLLM':value})['VLLM']
    except MissingSecret:
        logging.getLogger('xiaozhi.worker.vision').warning('Vision credential unavailable; camera capability remains disabled')
        return None
    key = value.get('api_key')
    if not key or (isinstance(key, str) and key.casefold().startswith(('your_', 'your-', '\u4f60'))):
        logging.getLogger('xiaozhi.worker.vision').warning('Vision credential not configured; camera capability remains disabled')
        return None
    return validate(value)


class GeminiVision:
    """Cancellable async adapter using the existing Gemini VLLM configuration."""
    def __init__(self, config):
        self.config = validate(config)
        self.client = None

    async def response(self, question, image):
        from google import genai
        from google.genai import types
        if self.client is None:
            self.client = genai.Client(api_key=self.config['api_key'])
        options = {key:value for key, value in self.config.items()
                   if key in {'temperature', 'top_p', 'top_k', 'max_output_tokens'} and value is not None}
        options.setdefault('max_output_tokens', 256)
        resolution = self.config.get('media_resolution') or 'medium'
        options['media_resolution'] = getattr(types.MediaResolution, 'MEDIA_RESOLUTION_' + resolution.upper())
        if self.config.get('thinking_level'):
            options['thinking_config'] = types.ThinkingConfig(thinking_level=self.config['thinking_level'])
        options['automatic_function_calling'] = types.AutomaticFunctionCallingConfig(disable=True)
        result = await self.client.aio.models.generate_content(
            model=self.config['model_name'],
            contents=[question + '\nReply in ' + self.config.get('response_language', 'English') + '.',
                      types.Part.from_bytes(data=image, mime_type='image/jpeg')],
            config=types.GenerateContentConfig(**options))
        return result.text if result is not None else None

    async def close(self):
        if self.client is not None:
            await self.client.aio.aclose()
            self.client.close()
            self.client = None
