"""Lightweight Desk MQTT bootstrap; no provider, firmware updater or Vision runtime."""
import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import web

PROTOCOL = 'xiaozhi-mqtt-bootstrap-v1'
MAX_REQUEST_BYTES = 16384
MAC = re.compile(r'[0-9a-f]{2}(?::[0-9a-f]{2}){5}')


def decode(data):
    def unique(pairs):
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError('Duplicate bootstrap fields')
        return value
    return json.loads(data, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite bootstrap value')))


@dataclass(frozen=True)
class MqttBootstrapConfig:
    signature_key: str = field(repr=False)
    port: int = 1883
    state_file: str = '/etc/xiaozhi-ingress.json'
    allowed_devices: tuple[str, ...] = ()

    @classmethod
    def from_env(cls):
        enabled = os.environ.get('XIAOZHI_MQTT_BOOTSTRAP_ENABLED', 'false')
        if enabled not in ('true', 'false'):
            raise ValueError('Invalid bootstrap enable flag')
        if enabled == 'false':
            return None
        try:
            key = os.environ.get('XIAOZHI_BOOTSTRAP_MQTT_SIGNATURE_KEY', '')
            port = int(os.environ.get('XIAOZHI_BOOTSTRAP_MQTT_PORT', '1883'))
            path = os.environ.get('XIAOZHI_BOOTSTRAP_INGRESS_STATE_FILE', '/etc/xiaozhi-ingress.json')
            allowed = decode(os.environ.get('XIAOZHI_BOOTSTRAP_ALLOWED_DEVICES', '[]'))
            if (not key.strip() or len(key) > 4096 or any(c in key for c in '\x00\r\n')
                    or not 1 <= port <= 65535 or not Path(path).is_absolute()
                    or any(c in path for c in '\x00\r\n')
                    or not isinstance(allowed, list) or len(allowed) > 128
                    or any(not isinstance(mac, str) or not MAC.fullmatch(mac.lower()) for mac in allowed)):
                raise ValueError
        except (ValueError, TypeError, UnicodeError):
            raise ValueError('Invalid MQTT bootstrap deployment configuration') from None
        return cls(key, port, path, tuple(sorted(set(mac.lower() for mac in allowed))))


class MqttBootstrap:
    def __init__(self, reconciliation, config):
        self.reconciliation = reconciliation
        self.config = config

    def _context(self):
        service = self.reconciliation
        if not service.http_operational or not service.healthy:
            raise OSError('Validated bootstrap state unavailable')
        context = service.bootstrap_context
        if context is None:
            raise OSError('Validated bootstrap metadata unavailable')
        desired, hours = context
        path = Path(self.config.state_file)
        if path.is_symlink():
            raise ValueError('Invalid applied ingress state')
        with path.open('rb') as source:
            content = source.read(4097)
        if len(content) > 4096:
            raise ValueError('Invalid applied ingress state')
        snapshot = decode(content)
        if not isinstance(snapshot, dict) or set(snapshot) != {'vip'}:
            raise ValueError('Invalid applied ingress snapshot')
        vip = ipaddress.IPv4Address(snapshot['vip'])
        if vip.is_unspecified or vip.is_multicast or vip.is_loopback or vip.is_reserved:
            raise ValueError('Invalid applied ingress VIP')
        if desired != str(vip):
            raise ValueError('Desired and applied ingress VIP differ')
        if type(hours) not in (int, float) or not math.isfinite(hours) or not -12 <= hours <= 14:
            raise ValueError('Invalid bootstrap time zone')
        return str(vip), int(round(hours * 60))

    @staticmethod
    def _response(value, status=200):
        response = web.json_response(value, status=status)
        response.headers['Cache-Control'] = 'no-store, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        return response

    async def handle(self, request):
        # Identity headers are self-reported, as in the retained upstream/local
        # bootstrap contract. This endpoint belongs on the trusted management LAN.
        if request.query_string:
            return self._response({'error': 'invalid_bootstrap_request'}, 400)
        device = request.headers.get('device-id', '').lower()
        client = request.headers.get('client-id', '')
        operator_get = request.method == 'GET' and not device and not client
        if not operator_get:
            if not MAC.fullmatch(device) or not re.fullmatch(r'[A-Za-z0-9_-]{1,192}', client):
                return self._response({'error': 'invalid_device_identity'}, 400)
            if self.config.allowed_devices and device not in self.config.allowed_devices:
                return self._response({'error': 'device_not_allowed'}, 403)
        body = {}
        if request.method == 'POST':
            if request.content_type != 'application/json':
                return self._response({'error': 'expected_json'}, 415)
            try:
                data = await asyncio.wait_for(request.read(), timeout=2)
                if not 0 < len(data) <= MAX_REQUEST_BYTES:
                    raise ValueError
                body = decode(data)
                if not isinstance(body, dict):
                    raise ValueError
            except (ValueError, TypeError, UnicodeError, RecursionError, asyncio.TimeoutError):
                return self._response({'error': 'invalid_bootstrap_payload'}, 400)
        try:
            # Only a bounded applied-state file read remains. This read-only task
            # cannot mutate configuration after request cancellation.
            vip, timezone = await asyncio.to_thread(self._context)
        except (OSError, ValueError, TypeError, KeyError):
            return self._response({'protocol': PROTOCOL, 'ready': False, 'error': 'bootstrap_unavailable'}, 503)
        if operator_get:
            return self._response({'protocol': PROTOCOL, 'ready': True, 'conversation_runtime': False})
        mac_safe = device.replace(':', '_')
        client_id = f'GID_desk_robot@@@{mac_safe}@@@{client}'
        username = base64.b64encode(b'{"ip":"unknown"}').decode('ascii')
        password = base64.b64encode(hmac.new(self.config.signature_key.encode('utf-8'),
            f'{client_id}|{username}'.encode('utf-8'), hashlib.sha256).digest()).decode('ascii')
        application = body.get('application', {})
        version = application.get('version') if isinstance(application, dict) else None
        if not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9_.+\-]{1,64}', version):
            version = '0.0.0'
        # These are per-device MQTT credentials, never the signing key itself.
        # Firmware explicitly ignores firmware upgrade objects; no download exists.
        return self._response({
            'server_time': {'timestamp': int(time.time() * 1000), 'timezone_offset': timezone},
            'firmware': {'version': version, 'url': ''},
            'mqtt': {'endpoint': f'{vip}:{self.config.port}', 'client_id': client_id,
                     'username': username, 'password': password, 'publish_topic': 'device-server',
                     'subscribe_topic': f'devices/p2p/{mac_safe}', 'keepalive': 240},
        })
