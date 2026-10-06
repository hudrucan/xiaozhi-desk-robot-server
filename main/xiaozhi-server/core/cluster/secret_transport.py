"""Authenticated encryption for bounded node-local secret provisioning over HTTP."""
import base64
import ipaddress
import json
import os
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

PROTOCOL = 'xiaozhi-secret-peer-v1'
MAX_BYTES = 16384


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def decode(data):
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_BYTES:
        raise ValueError('Invalid secret message size')
    def unique(pairs):
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError('Duplicate secret message field')
        return value
    return json.loads(data, object_pairs_hook=unique)


@dataclass(frozen=True)
class SecretProvisionConfig:
    key: bytes = field(repr=False)
    nodes: tuple[tuple[str, str], ...]

    @classmethod
    def from_env(cls, port):
        key = os.environ.get('XIAOZHI_SECRET_PROVISION_KEY', '')
        nodes = os.environ.get('XIAOZHI_SECRET_PROVISION_NODES', '')
        if not key and not nodes:
            return None
        if not re.fullmatch('[0-9a-f]{64}', key):
            raise ValueError('Invalid secret provisioning deployment configuration')
        values = decode(nodes.encode())
        if not isinstance(values, dict) or len(values) != 3:
            raise ValueError('Secret provisioning requires three explicit peers')
        hosts = set()
        for node, endpoint in values.items():
            if not re.fullmatch('[A-Za-z0-9_-]{1,192}', node) or not isinstance(endpoint, str):
                raise ValueError('Invalid secret peer')
            url = urlsplit(endpoint)
            address = ipaddress.IPv4Address(url.hostname)
            if (url.scheme != 'http' or not address.is_private or address.is_loopback
                    or address.is_unspecified or address.is_multicast or address.is_link_local
                    or url.username is not None or url.password is not None or url.path
                    or url.query or url.fragment or url.port != port or str(address) in hosts):
                raise ValueError('Invalid private secret peer endpoint')
            hosts.add(str(address))
        return cls(bytes.fromhex(key), tuple(sorted(values.items())))

    def endpoint(self, node):
        return dict(self.nodes)[node]

    def address(self, node):
        return urlsplit(self.endpoint(node)).hostname


class SecretCipher:
    def __init__(self, config):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        self.config = config
        self.aes = AESGCM(config.key)

    def seal(self, action, source, target, operation, payload):
        header = {'protocol': PROTOCOL, 'action': action, 'source': source, 'target': target,
                  'operation': operation, 'issued_at': int(time.time())}
        nonce = os.urandom(12)
        ciphertext = self.aes.encrypt(nonce, canonical(payload), canonical(header))
        result = {**header, 'nonce': base64.b64encode(nonce).decode('ascii'),
                  'box': base64.b64encode(ciphertext).decode('ascii')}
        data = canonical(result)
        if len(data) > MAX_BYTES:
            raise ValueError('Secret message exceeds limit')
        return result

    def open(self, data, action, target, *, source=None, operation=None, remote=None):
        value = decode(data)
        expected = {'protocol', 'action', 'source', 'target', 'operation', 'issued_at', 'nonce', 'box'}
        if (not isinstance(value, dict) or set(value) != expected or value['protocol'] != PROTOCOL
                or value['action'] != action or value['target'] != target
                or value['source'] not in dict(self.config.nodes)
                or type(value['issued_at']) is not int or not -5 <= time.time()-value['issued_at'] <= 60
                or not isinstance(value['operation'], str) or not re.fullmatch('[0-9a-f]{32}', value['operation'])
                or (source is not None and value['source'] != source)
                or (operation is not None and value['operation'] != operation)
                or (remote is not None and remote != self.config.address(value['source']))):
            raise ValueError('Secret message authentication failed')
        header = {key: value[key] for key in expected - {'nonce', 'box'}}
        nonce = base64.b64decode(value['nonce'], validate=True)
        ciphertext = base64.b64decode(value['box'], validate=True)
        if len(nonce) != 12 or not 16 <= len(ciphertext) <= MAX_BYTES:
            raise ValueError('Invalid secret ciphertext')
        return value['source'], value['operation'], decode(self.aes.decrypt(nonce, ciphertext, canonical(header)))
