"""Root-owned local runtime installer; fixed paths/services, no remote shell.

The control plane talks to a permission-restricted Unix socket. Provider bundles
are prepared offline from validated cache; all cores stay stopped until all
workers have the same revision. A durable journal allows coordinated recovery.
"""
import asyncio
import grp
import hashlib
import json
import logging
import os
import pwd
import signal
import shutil
import socket
import stat
import struct
import tempfile
import time
from pathlib import Path

from core.cluster import runtime_protocol as wire

LOGGER = logging.getLogger('xiaozhi.runtime_apply')
CONFIG = Path('/etc/xiaozhi-runtime-apply.json')
STATE = Path('/var/lib/xiaozhi-runtime-apply')
SOCKET = '/run/xiaozhi-runtime-apply/apply.sock'
PERMITS = Path('/run/xiaozhi-runtime-apply')
BUNDLES = {'llm': '/etc/xiaozhi-worker-llm/config.json',
           'asr': '/etc/xiaozhi-worker-asr/config.json',
           'tts': '/etc/xiaozhi-worker-tts/config.json',
           'core': '/etc/xiaozhi-core-tts/config.json',
           'env': '/etc/xiaozhi-core-runtime/revision.env'}
LIMIT = 512 * 1024


def read_private(path, limit=LIMIT):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o027 or info.st_size > limit:
            raise ValueError('Invalid private deployment file')
        return stream.read(limit + 1)


def atomic(path, content, gid=0, mode=0o600):
    path = Path(path)
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError('Deployment writes must not traverse symlinks')
    fd, temporary = tempfile.mkstemp(prefix='.runtime-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            os.fchown(stream.fileno(), 0, gid)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Installer:
    def __init__(self, config, client, session, *, root=STATE, bundles=BUNDLES):
        from core.cluster.nats_config import validate_worker_id
        if set(config) != {'node_id', 'nodes', 'source_root', 'core_host', 'core_port',
                           'control_plane_user', 'worker_group', 'core_group'}:
            raise ValueError('Invalid installer deployment configuration')
        for node in config['nodes']:
            validate_worker_id(node)
        validate_worker_id(config['node_id'])
        if (len(config['nodes']) != 3 or len(set(config['nodes'])) != 3
                or config['node_id'] not in config['nodes']):
            raise ValueError('Installer requires three deployed nodes')
        import ipaddress
        address = ipaddress.IPv4Address(config['core_host'])
        if not address.is_private or address.is_unspecified or address.is_loopback or address.is_multicast:
            raise ValueError('Invalid local private core address')
        if type(config['core_port']) is not int or not 1024 <= config['core_port'] <= 65535:
            raise ValueError('Invalid core port')
        self.config, self.client, self.session = config, client, session
        self.root, self.bundles = Path(root), {key: Path(value) for key, value in bundles.items()}
        self.source = Path(config['source_root'])
        if not self.source.is_absolute() or any(p.is_symlink() for p in (self.source, *self.source.parents)):
            raise ValueError('Invalid deployment source root')
        self.worker_gid = grp.getgrnam(config['worker_group']).gr_gid
        self.core_gid = grp.getgrnam(config['core_group']).gr_gid
        self.lock = asyncio.Lock()
        self.journal = None
        if (self.root / 'journal.json').exists():
            self.journal = json.loads(read_private(self.root / 'journal.json', wire.MAX_BYTES))
        self.operations = set()
        self.requests = {}
        if (self.root / 'requests.json').exists():
            self.requests = json.loads(read_private(self.root / 'requests.json', wire.MAX_BYTES))

    def remember(self, request_id):
        # Persist deduplication across agent/control-plane restarts. The peer
        # cipher accepts requests for 60 seconds; retain IDs for twice that.
        now = time.time()
        self.requests = {key: timestamp for key, timestamp in self.requests.items()
                         if now - timestamp <= 120}
        if request_id in self.requests:
            return False
        if len(self.requests) >= 128:
            raise ValueError('Runtime command replay budget exhausted')
        self.requests[request_id] = now
        atomic(self.root / 'requests.json', json.dumps(self.requests).encode())
        return True

    def save(self, **fields):
        self.journal.update(fields)
        atomic(self.root / 'journal.json', json.dumps(self.journal).encode())

    def directory(self):
        return self.root / self.journal['operation']

    def store(self):
        from config.bootstrap import load_bootstrap
        from config.google_drive_config import GoogleDriveConfigStore
        from core.cluster.runtime_apply_secrets import CacheSecrets
        bootstrap = load_bootstrap(self.source / 'data/bootstrap.yaml')
        if bootstrap['node_id'] != self.config['node_id']:
            raise ValueError('Local Cloud identity differs')
        return GoogleDriveConfigStore(bootstrap, transport=object(), default_path=str(self.source / 'config.yaml'),
            cache_dir=self.source / 'data/cloud-config',
            secret_provider=CacheSecrets(bootstrap['node_id'], self.source / 'data/node-secrets',
                pwd.getpwnam(self.config['control_plane_user']).pw_uid))

    def require_revision(self, revision):
        from config.cloud_layers import shared_cluster
        store = self.store()
        snapshot = store._read_cache('desired.json')
        if snapshot['payload']['manifest']['revision'] != revision or not shared_cluster(snapshot['payload']['object']):
            raise ValueError('Cloud desired revision differs')

    def prepare(self, operation, revision):
        if self.journal and self.journal['operation'] == operation:
            if self.journal['revision'] != revision:
                raise ValueError('Operation revision differs')
            return
        if self.journal and self.journal['state'] not in wire.TERMINAL:
            raise ValueError('An existing operation requires recovery')
        from core.cluster.llm_config import load_bundle as load_llm
        from core.cluster.asr_config import load_bundle as load_asr
        from core.cluster.tts_config import load_bundle as load_tts, MAX_BUNDLE
        from config.config_loader import merge_configs
        from export_worker_llm_config import build_bundle as llm, write_private
        from export_worker_asr_config import build_bundle as asr
        from export_worker_tts_config import build_bundle as tts
        from provision_core_soundbank import provision
        old = {'llm': load_llm(str(self.bundles['llm']), self.config['node_id']),
               'asr': load_asr(self.bundles['asr'], self.config['node_id']),
               'tts': load_tts(str(self.bundles['tts']), self.config['node_id']),
               'core': load_tts(str(self.bundles['core']), self.config['node_id'])}
        revisions = {value['revision'] for value in old.values()}
        if len(revisions) != 1:
            raise ValueError('Existing deployment revisions differ')
        previous = revisions.pop()
        if revision < previous:
            raise ValueError('Runtime downgrade is unsupported')
        store = self.store()
        snapshot = store._read_cache('desired.json')
        from config.cloud_layers import shared_cluster
        if snapshot['payload']['manifest']['revision'] != revision or not shared_cluster(snapshot['payload']['object']):
            raise ValueError('Cloud desired revision differs')
        config = merge_configs(*store._resolve(snapshot['payload']['object']))
        selected = config['selected_module']
        source_asr = config['ASR'][selected['ASR']]
        source_tts = config['TTS'][selected['TTS']]
        # Model/provider changes require a reviewed Ansible dependency/asset
        # deployment. Settings can tune the already installed voice adapters.
        if (source_asr['type'] != old['asr']['provider']['type']
                or source_tts['type'] != 'sherpa'
                or (self.source / source_tts['model_dir']).resolve() != Path(old['tts']['model_root'])):
            raise ValueError('Selected provider/model is not deployed')
        if source_asr['type'] == 'sherpa_streaming':
            for key, asset in old['asr']['provider']['models'].items():
                if (self.source / source_asr['model_dir'] / source_asr[key]).resolve() != Path(asset['path']):
                    raise ValueError('Selected ASR assets are not deployed')
        candidate = {'llm': llm(store, revision),
            'asr': asr(store, old['asr']['vad']['model_path'], old['asr']['vad']['sha256'], revision, self.source),
            'tts': tts(store, self.source, revision, sorted(self.config['nodes']))}
        if (candidate['llm']['provider']['type'] != old['llm']['provider']['type']
                or candidate['tts']['files'] != old['tts']['files']
                or candidate['asr']['provider'].get('models') != old['asr']['provider'].get('models')):
            raise ValueError('New dependencies/model assets need Ansible deployment')
        candidate['core'], _ = provision(candidate['tts'],
            self.source / 'data/cloud-soundbank/objects', self.bundles['core'].parent / 'soundbank', self.core_gid)
        env = read_private(self.bundles['env'], 65536).decode('utf-8')
        import re
        pattern = r'(?m)^XIAOZHI_CORE_ASR_REVISION=(?:"?[0-9]+"?)$'
        matches = re.findall(pattern, env)
        if len(matches) != 1 or int(matches[0].split('=', 1)[1].strip('"')) != previous:
            raise ValueError('Core environment does not match installed bundles')
        candidate_env = re.sub(pattern, 'XIAOZHI_CORE_ASR_REVISION=' + str(revision), env).encode()
        directory = self.root / operation
        directory.mkdir(mode=0o700, exist_ok=True)
        backups = {}
        for name, path in self.bundles.items():
            content = read_private(path, 65536 if name == 'env' else LIMIT)
            atomic(directory / ('old-' + name), content)
            backups[name] = hashlib.sha256(content).hexdigest()
            if name == 'env':
                atomic(directory / ('new-' + name), candidate_env)
            else:
                write_private(directory / ('new-' + name), candidate[name], max_bytes=MAX_BUNDLE)
        old_operation = self.journal['operation'] if self.journal else None
        self.journal = {'operation': operation, 'revision': revision, 'previous_revision': previous,
                        'state': 'prepared', 'candidate_fingerprint': candidate['tts']['fingerprint'],
                        'backup_hashes': backups}
        self.save()
        # Keep only this transaction's rollback material. A new operation is
        # acquired only after the previous cluster operation is terminal.
        if old_operation is not None and old_operation != operation:
            old_directory = self.root / old_operation
            if old_directory.is_dir() and not old_directory.is_symlink():
                try:
                    shutil.rmtree(old_directory)
                except OSError:
                    LOGGER.warning('Previous private runtime backup cleanup deferred')

    def install(self, old=False):
        contents = {}
        for name, path in self.bundles.items():
            content = read_private(self.directory() / (('old-' if old else 'new-') + name), LIMIT)
            if old and hashlib.sha256(content).hexdigest() != self.journal['backup_hashes'][name]:
                raise ValueError('Backup integrity differs')
            contents[name] = content
        for name, content in contents.items():
            path = self.bundles[name]
            gid = 0 if name == 'env' else self.core_gid if name == 'core' else self.worker_gid
            atomic(path, content, gid, 0o600 if name == 'env' else 0o640)

    async def service(self, action, unit):
        if action not in {'stop', 'start'} or unit not in {'xiaozhi-worker', 'xiaozhi-core'}:
            raise ValueError('Unsupported service operation')
        permit = PERMITS / (unit + '-start.json')
        if action == 'start':
            await asyncio.to_thread(atomic, permit, json.dumps({'operation': self.journal['operation'],
                                        'expires_at': time.time() + 30}).encode())
        process = None
        try:
            process = await asyncio.create_subprocess_exec('/usr/bin/systemctl', action, unit + '.service',
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            if await asyncio.wait_for(process.wait(), 180) != 0:
                raise ValueError('Service operation unavailable')
        except BaseException:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            raise
        finally:
            if action == 'start':
                permit.unlink(missing_ok=True)

    async def probe(self):
        worker_revision = core_revision = None
        worker_ready = core_ready = False
        try:
            from core.cluster.tts_config import load_bundle
            from core.cluster import tts_protocol as tts
            bundle = await asyncio.to_thread(load_bundle, str(self.bundles['tts']), self.config['node_id'])
            request = {'protocol': tts.PROTOCOL, 'op': 'status', 'core_id': self.config['node_id'],
                       'revision': bundle['revision'], 'fingerprint': bundle['fingerprint']}
            message = await self.client.request(tts.subject(self.config['node_id']), json.dumps(request).encode(), timeout=1)
            value, audio = tts.unpack(message.data)
            if (not audio and set(value) == {'protocol', 'worker_id', 'revision', 'fingerprint', 'state'}
                    and value['protocol'] == tts.PROTOCOL and value['worker_id'] == self.config['node_id']
                    and type(value['revision']) is int and value['revision'] > 0
                    and value['state'] in {'idle', 'busy'}):
                worker_revision = value['revision']
                worker_ready = value['fingerprint'] == bundle['fingerprint'] and worker_revision == bundle['revision']
        except Exception:
            pass
        try:
            url = f"http://{self.config['core_host']}:{self.config['core_port']}/status"
            async with self.session.get(url, allow_redirects=False) as response:
                data = bytearray()
                async for chunk in response.content.iter_chunked(4096):
                    data.extend(chunk)
                    if len(data) > wire.MAX_BYTES:
                        raise ValueError('Core status exceeds limit')
                value = wire.decode(bytes(data))
                if (response.status == 200 and value.get('protocol') == 'xiaozhi-core-transport-v1'
                        and value.get('core_id') == self.config['node_id']):
                    revision = value.get('tts', {}).get('revision')
                    if type(revision) is int and revision > 0:
                        core_revision = revision
                        core_ready = value.get('status') == 'ready' and value.get('worker_rpc', {}).get('state') == 'connected'
        except Exception:
            pass
        return worker_revision, core_revision, worker_ready, core_ready

    async def status(self):
        worker, core, worker_ready, core_ready = await self.probe()
        journal = self.journal or {}
        return {'node_id': self.config['node_id'], 'state': journal.get('state', 'idle'),
                'operation': journal.get('operation'), 'revision': journal.get('revision'),
                'previous_revision': journal.get('previous_revision'),
                'candidate_fingerprint': journal.get('candidate_fingerprint'),
                'worker_revision': worker, 'core_revision': core,
                'ready': worker_ready and core_ready and worker == core, 'code': None}

    async def wait_ready(self, component, revision):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            worker, core, worker_ready, core_ready = await self.probe()
            if (worker == revision and worker_ready if component == 'worker' else
                    worker == core == revision and worker_ready and core_ready):
                return
            await asyncio.sleep(2)
        raise ValueError('Runtime readiness unavailable')

    async def dispatch(self, value):
        value = wire.command(value)
        action, operation, revision = (value[key] for key in ('action', 'operation', 'revision'))
        if action == 'job':
            raise ValueError('Job metadata belongs to the control plane')
        if action == 'status':
            return await self.status()
        if action == 'check':
            await asyncio.to_thread(self.require_revision, revision)
            return await self.status()
        async with self.lock:
            if not await asyncio.to_thread(self.remember, value['request_id']):
                return await self.status()
            if action == 'prepare':
                if self.journal is None or self.journal['operation'] != operation:
                    worker, core, worker_ready, core_ready = await self.probe()
                    if not worker_ready or not core_ready or worker != core:
                        raise ValueError('Existing running revisions are not confirmed')
                await asyncio.to_thread(self.prepare, operation, revision)
                return await self.status()
            if not self.journal or self.journal['operation'] != operation or self.journal['revision'] != revision:
                raise ValueError('No matching prepared operation')
            phase = self.journal['state']
            if action == 'quiesce':
                # Journal before the first service mutation: a crashed coordinator
                # must not leave an apparently discardable prepared operation.
                await asyncio.to_thread(self.save, state='quiesced')
                await self.service('stop', 'xiaozhi-core')
                await self.service('stop', 'xiaozhi-worker')
            elif action == 'install':
                if phase != 'quiesced':
                    raise ValueError('Runtime is not quiesced')
                await asyncio.to_thread(self.require_revision, revision)
                await asyncio.to_thread(self.install)
                await asyncio.to_thread(self.save, state='installed')
            elif action in {'worker', 'old_worker'}:
                if phase != ('installed' if action == 'worker' else 'restored'):
                    raise ValueError('Bundle phase differs')
                await self.service('start', 'xiaozhi-worker')
                await self.wait_ready('worker', revision if action == 'worker' else self.journal['previous_revision'])
                await asyncio.to_thread(self.save, state='worker_started' if action == 'worker' else 'old_worker_started')
            elif action in {'core', 'old_core'}:
                if phase != ('worker_started' if action == 'core' else 'old_worker_started'):
                    raise ValueError('Worker phase differs')
                if action == 'core':
                    await asyncio.to_thread(self.require_revision, revision)
                await self.service('start', 'xiaozhi-core')
                await self.wait_ready('core', revision if action == 'core' else self.journal['previous_revision'])
                await asyncio.to_thread(self.save, state='core_started' if action == 'core' else 'old_core_started')
            elif action == 'restore':
                if phase != 'quiesced':
                    raise ValueError('Rollback requires stopped runtimes')
                await asyncio.to_thread(self.install, True)
                await asyncio.to_thread(self.save, state='restored')
            elif action in {'finish', 'rolled_back'}:
                expected = 'core_started' if action == 'finish' else 'old_core_started'
                # Preparation failure needs only to release its journal lock.
                if phase == 'prepared' and action == 'rolled_back':
                    pass
                elif phase != expected and phase != ('complete' if action == 'finish' else 'rolled_back'):
                    raise ValueError('Runtime is not verified')
                await asyncio.to_thread(self.save, state='complete' if action == 'finish' else 'rolled_back')
            return await self.status()


async def run():
    from aiohttp import ClientSession, ClientTimeout
    from nats.aio.client import Client
    from core.cluster.nats_config import NatsConnectionConfig
    config = json.loads(read_private(CONFIG, wire.MAX_BYTES))
    allowed_uid = pwd.getpwnam(config['control_plane_user']).pw_uid
    client = Client()
    nats = NatsConnectionConfig.from_env()
    async def safe_error(error):
        LOGGER.warning('Runtime agent NATS unavailable')
    await client.connect(servers=list(nats.servers), user=nats.username, password=nats.password,
        error_cb=safe_error, allow_reconnect=True, max_reconnect_attempts=-1,
        reconnect_time_wait=2, connect_timeout=3, pending_size=64 * 1024)
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    async with ClientSession(trust_env=False, timeout=ClientTimeout(total=2)) as session:
        installer = Installer(config, client, session)
        async def serve(reader, writer):
            task = None
            try:
                peer = writer.get_extra_info('socket')
                _, uid, _ = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid not in {0, allowed_uid}:
                    raise ValueError('Unauthorized local caller')
                data = await asyncio.wait_for(reader.readline(), 2)
                value = wire.command(wire.decode(data))
                task = asyncio.create_task(installer.dispatch(value))
                installer.operations.add(task)
                task.add_done_callback(installer.operations.discard)
                try:
                    result = await asyncio.shield(task)
                except asyncio.CancelledError:
                    await asyncio.shield(task)
                    raise
                result = {'ok': True, 'node': result}
            except Exception:
                result = {'ok': False, 'code': 'runtime_unavailable'}
                LOGGER.warning('Runtime apply operation unavailable; validate deployment, assets and desired revision')
            try:
                writer.write(json.dumps(result).encode() + b'\n')
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        if Path(SOCKET).exists():
            Path(SOCKET).unlink()
        server = await asyncio.start_unix_server(serve, SOCKET, limit=wire.MAX_BYTES + 1)
        os.chown(SOCKET, 0, grp.getgrnam(config['control_plane_user']).gr_gid)
        os.chmod(SOCKET, 0o660)
        async with server:
            await stop.wait()
        if installer.operations:
            await asyncio.gather(*tuple(installer.operations), return_exceptions=True)
    await client.close()


def boot_guard(component):
    if component not in {'worker', 'core'}:
        return 1
    if not (STATE / 'journal.json').exists():
        return 0
    journal = json.loads(read_private(STATE / 'journal.json', wire.MAX_BYTES))
    if journal['state'] in wire.TERMINAL or journal['state'] == 'prepared':
        return 0
    permit = json.loads(read_private(PERMITS / ('xiaozhi-' + component + '-start.json'), wire.MAX_BYTES))
    return 0 if (permit.get('operation') == journal['operation']
                 and type(permit.get('expires_at')) in {int, float}
                 and 0 < permit['expires_at'] - time.time() <= 30) else 1


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    logging.getLogger('nats').addHandler(logging.NullHandler())
    logging.getLogger('nats').propagate = False
    try:
        import sys
        if len(sys.argv) == 3 and sys.argv[1] == '--boot-guard':
            raise SystemExit(boot_guard(sys.argv[2]))
        if len(sys.argv) != 1:
            raise ValueError('Unexpected runtime agent arguments')
        asyncio.run(run())
    except Exception:
        LOGGER.error('Runtime agent unavailable; validate root-owned deployment configuration')
        raise SystemExit(1) from None
