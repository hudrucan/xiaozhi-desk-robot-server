"""Provider-free Cloud Memory owner; reads cached snapshots, writes CAS-last."""
import asyncio
import copy
import hashlib
import logging
import time
from collections import OrderedDict

from config.cloud_memory import CloudMemoryStore
from config.config_loader import merge_configs
from core.explicit_memory import ExplicitMemory
from core.memory_storage import MemoryConflict, MemoryReadOnly
from . import memory_protocol as wire
from .protocol import valid_reply_subject

LOGGER = logging.getLogger('xiaozhi.control_plane.memory')


class MemoryService:
    def __init__(self, reconciliation):
        self.reconciliation = reconciliation
        self.store = reconciliation.store
        self.node = self.store.bootstrap['node_id']
        self.source = self.fingerprint = self.backend = None
        self.nodes = set()
        self.lock = asyncio.Lock()
        self.wake = asyncio.Event()
        self.stopping = False
        self.task = None
        self.jobs = set()
        self.seen = OrderedDict()
        self.state = 'not_started'

    def status(self):
        status = self.backend.status() if self.backend is not None else {}
        authority = (hashlib.sha256(wire.encode({'folder': self.backend.folder_id,
            'manifest': self.backend.manifest_id}, 16384)).hexdigest() if self.backend is not None else None)
        return {'state': self.state, 'authority_fingerprint': authority,
                'memory_revision': status.get('memory_revision'),
                'sync_state': status.get('sync_state'), 'writer_node_id': status.get('writer_node_id'),
                'write_mode': status.get('write_mode'), 'writable': status.get('writable', False)}

    async def owned(self, function):
        # Never abandon a thread while it may commit Cloud CAS. Shutdown waits
        # for its result; cancellation does not replay an uncertain write.
        task = asyncio.create_task(asyncio.to_thread(function))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not task.cancelled():
                task.exception()
            raise

    def refresh_blocking(self):
        with self.store.locked():
            snapshot = self.store.desired_snapshot
            from config.cloud_layers import shared_cluster
            if not snapshot or not shared_cluster(snapshot['payload']['object']):
                raise ValueError('Memory requires shared V2 configuration')
            config = merge_configs(self.store.defaults_unlocked(), self.store.read_unlocked())
            source, digest = wire.policy(config)
            self.nodes = set(snapshot['payload']['object']['layers']['nodes'])
        if source is None:
            self.source = self.fingerprint = self.backend = None
            self.state = 'disabled'
            return
        if digest != self.fingerprint or self.backend is None:
            backend = CloudMemoryStore(self.store.bootstrap, self.store.transport,
                self.store.cache_dir.parent / 'cloud-memory', source, materialize=False, shared_writes=True)
            backend.sync()
            self.source, self.fingerprint, self.backend = source, digest, backend
        else:
            self.backend.sync()
        self.state = 'ready'

    async def refresh(self):
        async with self.lock:
            try:
                await self.owned(self.refresh_blocking)
            except Exception:
                self.state = 'unavailable'
                LOGGER.warning('Cloud Memory sync unavailable; writes still require live CAS')

    def request_refresh(self):
        if not self.stopping:
            self.wake.set()

    async def on_hint(self, message):
        try:
            hint = wire.decode(message.data, 256)
            if (set(hint) != {'protocol', 'revision'} or hint['protocol'] != wire.PROTOCOL
                    or type(hint['revision']) is not int or hint['revision'] < 1):
                return
            current = self.status()['memory_revision'] or 0
            if hint['revision'] > current:
                self.request_refresh()
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            pass

    async def register(self, client):
        await client.subscribe(wire.subject(self.node), cb=self.receive,
            pending_msgs_limit=8, pending_bytes_limit=8 * wire.MAX_BYTES)
        await client.subscribe(wire.CHANGED, cb=self.on_hint,
            pending_msgs_limit=64, pending_bytes_limit=64 * 256)
        self.request_refresh()

    def start(self):
        self.task = asyncio.create_task(self.run())

    async def run(self):
        self.wake.set()
        while not self.stopping:
            await self.wake.wait()
            self.wake.clear()
            if self.stopping:
                break
            await self.refresh()
            try:
                await asyncio.wait_for(self.wake.wait(), self.reconciliation.config.reconcile_interval)
            except asyncio.TimeoutError:
                self.wake.set()

    def response(self, value, *, text='', project=None, error=None):
        status = self.status()
        result = {'protocol': wire.PROTOCOL, 'request_id': value['request_id'],
            'status': 'error' if error else 'ok', 'text': text, 'active_project': project,
            'writer_node_id': status['writer_node_id'], 'memory_revision': status['memory_revision'], 'error': error}
        data = wire.encode(result, wire.MAX_BYTES)
        wire.reply(data, value['request_id'])
        return data

    def execute(self, value):
        if value['deadline_ms'] <= time.time()*1000:
            return self.response(value, error='memory_expired')
        if value['core_id'] not in self.nodes or self.node not in self.nodes or self.backend is None:
            return self.response(value, error='memory_unavailable')
        if value['policy'] != self.fingerprint:
            return self.response(value, error='memory_policy_mismatch')
        action, args = value['action'], value['arguments']
        memory = ExplicitMemory(self.source)
        memory.bind_storage(self.backend)
        memory.init_memory(value['device_id'], None)
        context = copy.deepcopy(value['context'])
        project = memory.resolve_active_project(args.get('content', ''), context['recent_messages'], context['active_project'])
        context['active_project'] = project
        responses = self.source.get('responses', {})
        if action == 'context':
            text = memory.recall(args['content'], context=context)
        elif action == 'recall':
            text = (memory.recall(args['content'], context=context) if memory.recall_enabled else
                    responses.get('recall_disabled', 'Memory recall is disabled.'))
            text = text or responses.get('not_found', 'I could not find a matching memory.')
        elif action == 'list':
            text = '\n'.join('- ' + entry for entry in memory.list_entries())
            text = text or responses.get('empty', 'I do not have any saved memories yet.')
        else:
            # Revalidate against the live manifest just before a mutation. Never
            # retry a CAS conflict or an uncertain mutation.
            memory.sync_memory()
            if value['deadline_ms'] <= time.time()*1000:
                return self.response(value, error='memory_expired')
            if action == 'update':
                entry = next((item for item in memory.entries if item['id'] == args['entry_id']), None)
                if entry is None:
                    return self.response(value, error='memory_invalid')
                metadata = {key: entry[key] for key in ('type', 'project', 'entities', 'tags',
                            'importance', 'pinned', 'active', 'supersedes')}
                metadata.update({k: v for k, v in args.items() if k not in {'content', 'entry_id'}})
                memory.update_entry(args['entry_id'], args['content'], **metadata)
                text = responses.get('updated', 'I updated that memory.')
            elif action == 'delete':
                if not memory.delete_entry(args['entry_id']):
                    return self.response(value, error='memory_invalid')
                text = responses.get('deleted', 'I deleted that memory.')
            elif action == 'remember':
                metadata = {k:v for k,v in args.items() if k != 'content'}
                changed = memory.remember(args['content'], **metadata)
                text = responses.get('remembered', 'I will remember that.') if changed else responses.get('missing_content', 'No memory content was provided.')
            else:
                changed = memory.forget(args['content'])
                text = responses.get('forgotten', 'I forgot that.') if changed else responses.get('not_found', 'I could not find a matching memory.')
        # Bounded UTF-8 results, without splitting a codepoint.
        text = text.encode()[:10000].decode('utf-8', errors='ignore')
        return self.response(value, text=text, project=project)

    async def receive(self, message):
        if self.stopping or not valid_reply_subject(message.reply) or not message.reply.startswith('_INBOX.'):
            return
        try:
            value = wire.request(message.data)
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            return
        now = time.monotonic()
        for key, expires in list(self.seen.items()):
            if expires <= now:
                del self.seen[key]
        key = (value['core_id'], value['request_id'])
        if key in self.seen or len(self.jobs) >= 4 or len(self.seen) >= 128:
            # Duplicate request IDs are never executed again, even with changed
            # arguments or after an uncertain response. Caller does not retry.
            client = self.reconciliation.client
            if client and client.is_connected:
                try:
                    await asyncio.wait_for(client.publish(message.reply,
                        self.response(value, error='memory_busy')), 2)
                except Exception:
                    LOGGER.warning('Memory admission reply unavailable')
            return
        self.seen[key] = now + 120
        task = asyncio.create_task(self.handle(message, value))
        self.jobs.add(task)
        task.add_done_callback(self.finished)

    def finished(self, task):
        self.jobs.discard(task)
        if not task.cancelled():
            task.exception()

    async def notify(self, before):
        revision = self.status()['memory_revision']
        committed = self.backend.commit_count if self.backend is not None else 0
        client = self.reconciliation.client
        if committed == before or client is None or not client.is_connected or self.stopping:
            return
        try:
            await asyncio.wait_for(client.publish(wire.CHANGED, wire.encode(
                {'protocol': wire.PROTOCOL, 'revision': revision}, 256)), 2)
        except Exception:
            LOGGER.warning('Memory hint unavailable; committed Cloud CAS remains authoritative')

    async def handle(self, message, value):
        async with self.lock:
            before = self.backend.commit_count if self.backend is not None else 0
            try:
                data = await self.owned(lambda: self.execute(value))
            except MemoryConflict:
                data = self.response(value, error='memory_conflict')
            except MemoryReadOnly:
                data = self.response(value, error='memory_read_only')
            except (ValueError, TypeError, KeyError):
                data = self.response(value, error='memory_invalid')
            except Exception:
                data = self.response(value, error='memory_unavailable')
            if value['action'] in wire.MUTATIONS:
                await self.notify(before)
            client = self.reconciliation.client
            if client and client.is_connected and not self.stopping:
                try:
                    await asyncio.wait_for(client.publish(message.reply, data), 2)
                except Exception:
                    LOGGER.warning('Memory reply unavailable; committed Cloud CAS remains authoritative')

    def settings(self, device_id, method='GET', body=None, entry_id=None):
        """Use cached reads and an explicit browser revision for mutations."""
        import re
        if self.backend is None or self.source is None or self.node not in self.nodes:
            raise RuntimeError('Memory unavailable')
        if method == 'GET':
            self.backend.sync()
        scopes = sorted(scope for scope in self.backend.scopes()
                        if re.fullmatch('[0-9a-f]{2}(?::[0-9a-f]{2}){5}', scope))
        if not device_id and len(scopes) == 1:
            device_id = scopes[0]
        if not device_id:
            return {'available': True, 'initialized': False, 'scopes': scopes,
                    'entries': [], **self.status(), 'reason': 'Select a device scope.'}
        if not re.fullmatch('[0-9a-f]{2}(?::[0-9a-f]{2}){5}', device_id):
            raise ValueError('Invalid device scope')
        memory = ExplicitMemory(self.source)
        memory.bind_storage(self.backend)
        memory.init_memory(device_id, None)
        if method != 'GET':
            if not isinstance(body, dict) or type(body.get('base_revision')) is not int:
                raise ValueError('A Memory base revision is required')
            args = {k: v for k, v in body.items() if k != 'base_revision'}
            if method == 'DELETE':
                if args:
                    raise ValueError('Invalid delete payload')
            else:
                # Reuse the same strict bounded metadata contract as MCP.
                wire.request(wire.encode({'protocol': wire.PROTOCOL, 'request_id': '0'*32,
                    'core_id': self.node, 'device_id': device_id, 'policy': self.fingerprint,
                    'deadline_ms': int(time.time()*1000)+1000, 'action': 'remember',
                    'arguments': args, 'context': {'recent_messages': [], 'active_project': None}}, wire.MAX_BYTES))
            memory.memory_revision = body['base_revision']
            if method == 'POST':
                memory.remember(**args)
            elif method == 'PUT':
                if not memory.update_entry(entry_id, **args):
                    raise LookupError('Memory entry not found')
            elif method == 'DELETE':
                if not memory.delete_entry(entry_id):
                    raise LookupError('Memory entry not found')
            else:
                raise ValueError('Invalid Memory operation')
        return {'available': True, 'scopes': sorted(set(scopes) | {device_id}),
                **memory.inspect_entries(), **self.status()}

    async def settings_operation(self, *args):
        if self.stopping or len(self.jobs) >= 4:
            raise RuntimeError('Memory unavailable')
        task = asyncio.current_task()
        self.jobs.add(task)
        try:
            async with self.lock:
                before = self.backend.commit_count if self.backend is not None else 0
                result = await self.owned(lambda: self.settings(*args))
                await self.notify(before)
                return result
        finally:
            self.jobs.discard(task)

    async def stop(self):
        self.stopping = True
        self.wake.set()
        if self.task:
            await self.task
        await asyncio.gather(*tuple(self.jobs), return_exceptions=True)
