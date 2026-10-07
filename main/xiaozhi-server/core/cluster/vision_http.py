"""Provider-free camera HTTP ingress and session-owned core upload handler."""
import asyncio
import hmac
import re
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout, web

from . import vision_protocol as wire

TOKEN = re.compile(r'([A-Za-z0-9_-]{1,192})\.([0-9a-f]{32})\.([0-9a-f]{32})')
MAX_BODY = wire.MAX_IMAGE + 16384


class UploadOwner:
    """Participate in the core's existing disconnect/shutdown invalidation."""
    def __init__(self, task):
        self.task = task

    def fail(self):
        self.task.cancel()


def failure(code, status):
    return web.json_response({'success':False, 'message':'Camera analysis is unavailable', 'code':code},
                             status=status, headers={'Cache-Control':'no-store'})


def token(request):
    authorization = request.headers.get('Authorization', '')
    match = TOKEN.fullmatch(authorization[7:]) if authorization.startswith('Bearer ') else None
    if request.query_string or match is None:
        raise web.HTTPUnauthorized()
    return match.groups()


async def relay(request, config):
    """A VIP backend routes to the owning core, never to a user-supplied URL."""
    node, _, _ = token(request)
    peers = config.secrets
    endpoint = dict(peers.nodes).get(node) if peers is not None else None
    if endpoint is None:
        return failure('vision_unavailable', 503)
    host = urlsplit(endpoint).hostname
    if ':' in host:
        host = '[' + host + ']'
    url = f'http://{host}:{config.diagnostic_core_port}/mcp/vision/explain'
    if request.content_type != 'multipart/form-data':
        return failure('vision_invalid_request', 415)
    # Drain no unbounded body and forward no arbitrary headers/redirects/proxy.
    # Streaming preserves the device's bounded multipart camera contract.
    async def body():
        size = 0
        async for chunk in request.content.iter_chunked(65536):
            size += len(chunk)
            if size > MAX_BODY:
                raise ValueError('Oversize camera upload')
            yield chunk
    headers = {name:request.headers[name] for name in ('Authorization', 'Content-Type', 'Device-Id', 'Client-Id')
               if name in request.headers}
    owner = asyncio.current_task()
    async def disconnected():
        while True:
            await asyncio.sleep(.1)
            if request.transport is None or request.transport.is_closing():
                owner.cancel()
                return
    watcher = asyncio.create_task(disconnected())
    try:
        async with ClientSession(trust_env=False, timeout=ClientTimeout(total=19)) as session:
            async with session.post(url, data=body(), headers=headers, allow_redirects=False) as response:
                data = bytearray()
                async for chunk in response.content.iter_chunked(4096):
                    data.extend(chunk)
                    if len(data) > wire.MAX_TEXT + 1024:
                        raise ValueError('Oversize camera result')
                # Reconstruct only the public camera contract from the core.
                value = wire.decode(bytes(data), wire.MAX_TEXT + 1024)
                if response.status == 200 and isinstance(value, dict) and set(value) == {'success', 'action', 'response'}:
                    if (value['success'] is True and value['action'] == 'RESPONSE'
                            and isinstance(value['response'], str) and len(value['response'].encode()) <= wire.MAX_TEXT):
                        return web.json_response(value, headers={'Cache-Control':'no-store'})
                return failure('vision_unavailable', response.status if response.status in {400, 401, 413, 415, 503} else 503)
    except asyncio.CancelledError:
        raise
    except Exception:
        return failure('vision_unavailable', 503)
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


async def upload(request, core):
    node, session_id, secret = token(request)
    mcp = core.mcps.get(session_id)
    voice = core.voices.get(session_id)
    if (node != core.config.node_id or core.stopping or mcp is None or voice is None
            or mcp.closed or not mcp.camera_active or mcp.vision is None
            or not hmac.compare_digest(secret, mcp.vision['token'].rsplit('.', 1)[1])
            or request.headers.get('Device-Id', '').lower() != mcp.device_id
            or request.headers.get('Client-Id', '') != mcp.client_id):
        return failure('vision_unauthorized', 401)
    if mcp.camera_upload or mcp.camera_consumed:
        return failure('vision_busy', 503)
    if request.content_type != 'multipart/form-data':
        return failure('vision_invalid_request', 415)
    generation = voice.generation
    if not core.worker_rpc.client.is_connected:
        return failure('vision_unavailable', 503)
    mcp.camera_upload = True
    mcp.camera_consumed = True
    task = asyncio.current_task()
    mcp.vision_tasks.add(task)
    owner = UploadOwner(task)
    core.worker_rpc.streams.add(owner)
    try:
        async with asyncio.timeout(3):
            reader = await request.multipart()
            question_field = await reader.next()
            if question_field is None or question_field.name != 'question':
                raise ValueError('Invalid camera question')
            question_data = bytearray()
            while chunk := await question_field.read_chunk(4096):
                question_data.extend(chunk)
                if len(question_data) > 2048:
                    raise ValueError('Oversize camera question')
            question = question_data.decode('utf-8')
            if not question.strip() or question != mcp.camera_question:
                raise ValueError('Camera question differs from the tool call')
            image_field = await reader.next()
            if image_field is None or image_field.name != 'file':
                raise ValueError('Invalid camera image')
            image = bytearray()
            while chunk := await image_field.read_chunk(65536):
                image.extend(chunk)
                if len(image) > wire.MAX_IMAGE:
                    raise ValueError('Oversize camera image')
            if not wire.jpeg(image) or await reader.next() is not None:
                raise ValueError('Invalid camera JPEG')
        from .vision_client import explain
        text = await explain(core.worker_rpc.client, core.voice_revision, question, bytes(image),
                             lambda worker: voice.record('vision_admitted', worker_id=worker))
        if generation != voice.generation or mcp.closed or not mcp.camera_active:
            return failure('vision_unavailable', 503)
        body = wire.encode({'success':True, 'action':'RESPONSE', 'response':text}, wire.MAX_TEXT)
        voice.record('vision_complete')
        return web.Response(body=body, content_type='application/json', headers={'Cache-Control':'no-store'})
    except asyncio.CancelledError:
        raise
    except Exception:
        voice.record('vision_failed', code='vision_unavailable')
        return failure('vision_unavailable', 503)
    finally:
        mcp.camera_upload = False
        mcp.vision_tasks.discard(task)
        core.worker_rpc.streams.discard(owner)
