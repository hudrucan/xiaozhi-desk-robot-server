"""All-node secret acknowledgements followed by Cloud CAS; no secrets over NATS."""
import asyncio
import hashlib
import hmac
import re
import uuid

from aiohttp import ClientSession, ClientTimeout

from config.cloud_layers import resolve_layers, shared_cluster
from config.cloud_secrets import REFERENCE, placeholder
from config.config_loader import merge_configs
from config.config_store import ConfigConflict, ConfigUnavailable
from .secret_transport import SecretCipher, canonical

GROUPS = {'ASR', 'LLM', 'VLLM', 'TTS', 'Memory', 'Intent'}


def validate_target(group, provider, field):
    if (group not in GROUPS or not isinstance(provider, str)
            or not re.fullmatch('[A-Za-z0-9_-]{1,192}', provider) or field != 'api_key'):
        raise ValueError('Unsupported provider credential field')


def validate_key(value):
    if (not isinstance(value, str) or not 1 <= len(value.encode('utf-8')) <= 4096
            or not value.isascii() or any(char.isspace() or ord(char) < 33 or ord(char) == 127 for char in value)
            or placeholder(value) or '${secret:' in value):
        raise ValueError('Enter a nonempty API key without whitespace or placeholders')


def fingerprint(name, value):
    return hashlib.sha256(canonical({'name': name, 'value': value})).hexdigest()


class ProvisionIncomplete(Exception):
    def __init__(self, nodes):
        self.nodes = nodes
        super().__init__('Secret provisioning was not confirmed by every node')


class SecretProvisioning:
    def __init__(self, reconciliation, config, *, exchange=None):
        self.reconciliation = reconciliation
        self.store = reconciliation.store
        self.config = config
        self.node = self.store.bootstrap['node_id']
        drive = self.store.bootstrap['google_drive']
        self.authority = hashlib.sha256(canonical({
            'folder': drive['folder_id'], 'manifest': drive['manifest_file_id']})).hexdigest()
        if self.node not in dict(config.nodes):
            raise ValueError('Local node must belong to secret provisioning membership')
        self.cipher = SecretCipher(config)
        self.exchange = exchange
        self.session = None
        self.busy = False
        self.stopping = False
        self.operations = set()

    def _target(self, group, provider, field):
        validate_target(group, provider, field)
        snapshot = self.store._desired_view()
        obj = snapshot['payload']['object']
        if not shared_cluster(obj) or not set(dict(self.config.nodes)) <= set(obj['layers']['nodes']):
            raise ValueError('Provisioning requires matching shared V2 membership')
        defaults = self.store._repo_defaults()
        effective = merge_configs(*resolve_layers(obj, self.node, defaults))
        if effective.get('selected_module', {}).get(group) != provider or field not in effective.get(group, {}).get(provider, {}):
            raise ValueError('Credential must belong to the currently selected provider')
        return obj, defaults

    def _prepare(self, group, provider, field, revision, name):
        with self.store.locked():
            self.store.prepare_commit_unlocked(revision)
            obj, defaults = self._target(group, provider, field)
            reference = '${secret:' + name + '}'
            candidate = self.store.prepare_secret_candidate_unlocked(group, provider, field, reference,
                tuple(node for node, _ in self.config.nodes))
            for node, _ in self.config.nodes:
                effective = merge_configs(*resolve_layers(candidate.cloud_object, node, defaults))
                if effective.get(group, {}).get(provider, {}).get(field) != reference:
                    raise ValueError('Node credential exceptions must be resolved before shared rotation')
            return candidate

    def _commit(self, candidate, revision):
        with self.store.locked():
            self.store.prepare_commit_unlocked(revision)
            self.store.commit_prepared_unlocked(candidate, revision)
            return revision + 1

    def _references(self, group, provider, field):
        with self.store.locked():
            obj, defaults = self._target(group, provider, field)
            references = {}
            for node, _ in self.config.nodes:
                value = merge_configs(*resolve_layers(obj, node, defaults)).get(group, {}).get(provider, {}).get(field)
                match = REFERENCE.fullmatch(value) if isinstance(value, str) else None
                references[node] = match[1] if match else None
            return references

    def local(self, action, payload):
        if not isinstance(payload, dict):
            raise ValueError('Invalid secret operation')
        if payload.get('authority') != self.authority:
            raise ValueError('Secret peer Cloud authority mismatch')
        name = payload.get('name')
        if not isinstance(name, str) or not REFERENCE.fullmatch('${secret:' + name + '}'):
            raise ValueError('Invalid credential reference')
        if action == 'store':
            if set(payload) != {'authority', 'name', 'value'} or not re.fullmatch('VALUE_[0-9a-f]{32}', name):
                raise ValueError('Invalid credential provision')
            validate_key(payload['value'])
            self.store.secrets.put_many({name: payload['value']})
            # Confirm a durable read-back, not merely a completed request.
            actual = self.store.secrets.get(name)
            if not hmac.compare_digest(actual, payload['value']):
                raise ValueError('Credential read-back mismatch')
            return {'status': 'stored', 'fingerprint': fingerprint(name, actual)}
        if action == 'status':
            if set(payload) != {'authority', 'name'}:
                raise ValueError('Invalid credential status request')
            try:
                value = self.store.secrets.get(name)
            except ValueError:
                return {'status': 'missing'}
            validate_key(value)
            return {'status': 'ready', 'fingerprint': fingerprint(name, value)}
        raise ValueError('Unknown secret operation')

    async def _http_exchange(self, node, envelope):
        if self.session is None:
            self.session = ClientSession(trust_env=False, timeout=ClientTimeout(total=4))
        async with self.session.post(self.config.endpoint(node) + '/internal/settings/secret',
                                     json=envelope, allow_redirects=False) as response:
            if response.status != 200:
                raise ValueError('Secret peer did not confirm')
            data = bytearray()
            async for chunk in response.content.iter_chunked(4096):
                data.extend(chunk)
                if len(data) > 16384:
                    raise ValueError('Secret acknowledgement exceeds limit')
            return bytes(data)

    async def _peer(self, node, action, payload, operation):
        payload = {**payload, 'authority': self.authority}
        if node == self.node:
            return await self.reconciliation.operation(self.local, action, payload)
        envelope = self.cipher.seal(action, self.node, node, operation, payload)
        exchange = self.exchange or self._http_exchange
        data = await asyncio.wait_for(exchange(node, envelope), timeout=5)
        _, _, response = self.cipher.open(data, 'stored' if action == 'store' else 'checked', self.node,
                                         source=node, operation=operation)
        if not isinstance(response, dict):
            raise ValueError('Invalid acknowledgement')
        return response

    async def provision(self, group, provider, field, value, revision):
        validate_target(group, provider, field)
        validate_key(value)
        if type(revision) is not int or revision < 1:
            raise ValueError('Expected a positive base revision')
        if self.busy or self.stopping:
            raise ConfigUnavailable('Secret provisioning is busy or stopping')
        self.busy = True
        task = asyncio.current_task()
        self.operations.add(task)
        name, operation = 'VALUE_' + uuid.uuid4().hex, uuid.uuid4().hex
        try:
            candidate = await self.reconciliation.operation(self._prepare, group, provider, field, revision, name)
            expected = fingerprint(name, value)
            async def one(node):
                try:
                    result = await self._peer(node, 'store', {'name': name, 'value': value}, operation)
                    if set(result) != {'status', 'fingerprint'} or result['status'] != 'stored' or not hmac.compare_digest(result['fingerprint'], expected):
                        raise ValueError('Credential acknowledgement mismatch')
                    return {'node_id': node, 'state': 'stored'}
                except Exception:
                    return {'node_id': node, 'state': 'unconfirmed'}
            nodes = await asyncio.gather(*(one(node) for node, _ in self.config.nodes))
            if any(node['state'] != 'stored' for node in nodes):
                raise ProvisionIncomplete(nodes)
            committed_revision = await self.reconciliation.operation(self._commit, candidate, revision)
            return {'committed': True, 'revision': committed_revision, 'nodes': nodes}
        finally:
            self.busy = False
            self.operations.discard(task)

    async def status(self, group, provider, field):
        if self.stopping:
            raise ConfigUnavailable('Secret provisioning is stopping')
        references = await self.reconciliation.operation(self._references, group, provider, field)
        operation = uuid.uuid4().hex
        async def one(node):
            if references[node] is None:
                return {'node_id': node, 'state': 'not_configured'}, None
            try:
                result = await self._peer(node, 'status', {'name': references[node]}, operation)
                if result == {'status': 'missing'}:
                    return {'node_id': node, 'state': 'missing'}, None
                if (set(result) != {'status', 'fingerprint'} or result['status'] != 'ready'
                        or not isinstance(result['fingerprint'], str) or not re.fullmatch('[0-9a-f]{64}', result['fingerprint'])):
                    raise ValueError('Invalid credential status')
                return {'node_id': node, 'state': 'ready'}, result['fingerprint']
            except Exception:
                return {'node_id': node, 'state': 'unconfirmed'}, None
        replies = await asyncio.gather(*(one(node) for node, _ in self.config.nodes))
        nodes = [value for value, _ in replies]
        consistent = (all(node['state'] == 'ready' for node in nodes)
                      and len(set(references.values())) == 1 and len({digest for _, digest in replies}) == 1)
        return {'nodes': nodes, 'ready': all(node['state'] == 'ready' for node in nodes), 'consistent': consistent}

    async def stop(self):
        self.stopping = True
        if self.operations:
            await asyncio.gather(*list(self.operations), return_exceptions=True)
        if self.session:
            await self.session.close()
