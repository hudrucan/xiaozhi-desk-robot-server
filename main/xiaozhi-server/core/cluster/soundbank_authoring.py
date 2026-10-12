"""Cloud Soundbank editing with applied TTS workers and verified immutable audio."""
import asyncio
import base64
import copy
import hashlib
import hmac
import io
import json
import re
import uuid
import tempfile
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from config.cloud_soundbank import _assets, _safe_target, _validate_p3
from config.config_loader import merge_configs
from config.config_store import ConfigConflict, ConfigUnavailable, canonical_bytes, checksum
from core.soundbank import (SoundbankAuthoringService, SoundbankError,
    resolve_soundbank_asset, sanitize_generation_settings, generation_fingerprint,
    validate_soundbank_cloud_metadata)
from core.utils import p3
from . import tts_protocol as wire
from .llm_protocol import encode
from .soundbank_reconciliation import (BoundedAssets, VerifiedCacheAssets,
    MAX_ASSETS, MAX_ASSET_BYTES, MAX_TOTAL_BYTES)
from .tts_client import TTSPool
from .worker_rpc import WorkerRpcError
from .secret_transport import SecretCipher


class AuthoringTransport(BoundedAssets):
    def upload_blob(self, *args):
        return self.transport.upload_blob(*args)


class AuthoringAssets(VerifiedCacheAssets):
    """Settings Save can reuse Cloud caches without writing runtime directories."""
    def _validated_local_assets(self, config):
        validate_soundbank_cloud_metadata(config)
        plan = list(_assets(config))
        if len(plan) > MAX_ASSETS:
            raise SoundbankError('Soundbank exceeds the asset limit')
        pending, total = [], 0
        for root, filename, path, metadata, optimized in plan:
            pointer = metadata.get('cloud')
            if pointer is not None:
                if pointer['size'] > MAX_ASSET_BYTES:
                    raise SoundbankError('Soundbank asset exceeds the byte limit')
                content = self._obtain(root, filename, path, metadata, optimized, config)
            else:
                actual = resolve_soundbank_asset(root, filename, require_file=True)
                if actual.stat().st_size > MAX_ASSET_BYTES:
                    raise SoundbankError('Soundbank asset exceeds the byte limit')
                content = actual.read_bytes()
            total += len(content)
            if not content or total > MAX_TOTAL_BYTES:
                raise SoundbankError('Soundbank exceeds the byte limit')
            _validate_p3(content, path, config, metadata, optimized)
            pending.append((root, path, metadata, optimized, content, (checksum(content), len(content))))
        return pending


class AudioCompiler(SoundbankAuthoringService):
    @staticmethod
    def _compile_p3(normalized_path, output_path, sample_rate):
        # Fixed mono PCM16 framing needs no NumPy or conversation provider import.
        import opuslib_next
        _, pcm = SoundbankAuthoringService._validate_wav(normalized_path, sample_rate)
        encoder = opuslib_next.Encoder(sample_rate, 1, opuslib_next.constants.APPLICATION_AUDIO)
        encoder.bitrate, encoder.complexity = 24000, 10
        frame_samples = sample_rate * p3.P3_FRAME_DURATION_MS // 1000
        frame_bytes = frame_samples * 2
        packets = [encoder.encode(pcm[offset:offset + frame_bytes].ljust(frame_bytes, b'\0'), frame_samples)
                   for offset in range(0, len(pcm), frame_bytes)]
        p3.validate_opus_packets(packets, sample_rate=sample_rate, frame_duration_ms=p3.P3_FRAME_DURATION_MS)
        p3.write_opus_file(output_path, packets)
        if p3.load_validated_opus_file(output_path, sample_rate=sample_rate) != packets:
            raise SoundbankError('Soundbank encoding did not verify', status=502)


class SoundbankAuthoring:
    def __init__(self, reconciliation, *, generate_pcm=None):
        self.reconciliation = reconciliation
        self.store = reconciliation.store
        peers = reconciliation.config.secrets
        self.nodes = tuple(node for node, _ in peers.nodes) if peers else ()
        self.key = peers.key if peers else None
        self.authority = checksum(canonical_bytes({'folder': self.store.folder_id,
                                                 'manifest': self.store.manifest_id}))
        assets = self.store.soundbank_assets
        self.assets = AuthoringAssets(AuthoringTransport(self.store.transport), self.store.folder_id,
                                      assets.cache_dir)
        self.store.soundbank_assets = self.assets
        self.workspace = self.assets.cache_dir.parent / 'drafts'
        self.generate_pcm = generate_pcm or self._worker_pcm
        self.lock = asyncio.Lock()
        self.tasks, self.streams = set(), set()
        self.stopping = False
        self.jobs = {}
        self.http = None
        self.cipher = SecretCipher(peers) if peers else None

    @property
    def enabled(self):
        return self.key is not None and len(self.nodes) == 3

    def disconnected(self):
        for stream in tuple(self.streams):
            stream.fail()

    async def stop(self):
        self.stopping = True
        self.disconnected()
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.http is not None:
            await self.http.close()

    async def start_job(self, action, **fields):
        if not self.enabled or self.stopping:
            raise SoundbankError('Cluster Soundbank authoring is unavailable', status=503)
        if action == 'generate':
            AudioCompiler._validate_text(fields.get('text'))
            AudioCompiler._validate_title(fields.get('title') or fields['text'])
        elif action != 'optimize':
            raise SoundbankError('Unknown Soundbank action')
        await self.reconciliation.operation(self.snapshot, fields.get('revision'))
        if self.lock.locked() or any(value['state'] == 'pending' for _, value in self.jobs.values()):
            raise SoundbankError('Soundbank authoring is busy; retry after it finishes', status=409)
        self.jobs = {key: value for key, value in self.jobs.items() if time.monotonic() - value[0] < 3600}
        if len(self.jobs) >= 32:
            oldest = min(self.jobs, key=lambda key: self.jobs[key][0])
            self.jobs.pop(oldest)
        job = uuid.uuid4().hex
        self.jobs[job] = (time.monotonic(), {'state': 'pending'})
        async def run():
            try:
                result = await getattr(self, action)(**fields)
                status = {'state': 'complete', 'result': result}
            except asyncio.CancelledError:
                status = {'state': 'failed', 'error': 'Soundbank authoring was stopped; retry generation', 'status': 503}
                raise
            except ConfigConflict:
                status = {'state': 'failed', 'error': 'Cloud changed; sync Settings before authoring', 'status': 409}
            except SoundbankError as error:
                status = {'state': 'failed', 'error': str(error), 'status': error.status}
            except Exception:
                status = {'state': 'failed', 'error': 'Soundbank authoring could not be confirmed; sync Settings and retry', 'status': 503}
            finally:
                self.jobs[job] = (time.monotonic(), status)
                self.tasks.discard(asyncio.current_task())
        task = asyncio.create_task(run(), name='soundbank-authoring')
        self.tasks.add(task)
        return {'job_id': job, 'poll_url': '/api/settings/soundbank/jobs/' + job}

    def local_job(self, job):
        if not isinstance(job, str) or not re.fullmatch('[0-9a-f]{32}', job):
            raise SoundbankError('Invalid Soundbank job')
        value = self.jobs.get(job)
        if value is None or time.monotonic() - value[0] >= 3600:
            return None
        return copy.deepcopy(value[1])

    async def job(self, job):
        local = self.local_job(job)
        if local is not None:
            return local
        from aiohttp import ClientSession, ClientTimeout
        if self.http is None:
            self.http = ClientSession(trust_env=False, timeout=ClientTimeout(total=3))
        node = self.store.bootstrap['node_id']
        async def peer(target):
            operation = uuid.uuid4().hex
            envelope = self.cipher.seal('soundbank-job', node, target, operation, {'job_id': job})
            try:
                async with self.http.post(self.reconciliation.config.secrets.endpoint(target)
                        + '/internal/settings/soundbank/job', json=envelope, allow_redirects=False) as response:
                    if response.status != 200:
                        raise ValueError
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(4096):
                        data.extend(chunk)
                        if len(data) > 16384:
                            raise ValueError
                    _, _, value = self.cipher.open(bytes(data), 'soundbank-job-result', node,
                        source=target, operation=operation)
                    if set(value) != {'job'}:
                        raise ValueError
                    return value['job']
            except Exception:
                return {'state': 'unavailable'}
        values = await asyncio.gather(*(peer(target) for target in self.nodes if target != node))
        found = [value for value in values if value and value.get('state') in {'pending', 'complete', 'failed'}]
        if found:
            return found[0]
        if any(value and value.get('state') == 'unavailable' for value in values):
            raise SoundbankError('Soundbank coordinator is temporarily unavailable; retry status', status=503)
        raise SoundbankError('Soundbank job expired or its node restarted; retry generation', status=404)

    def snapshot(self, revision=None):
        with self.store.locked():
            current = self.store.revision_unlocked()
            if revision is not None and current != revision:
                raise ConfigConflict('Cloud changed; sync Settings before authoring')
            config = copy.deepcopy(merge_configs(self.store.defaults_unlocked(), self.store.read_unlocked()))
            return current, config

    async def _worker_pcm(self, revision, text):
        client = self.reconciliation.client
        if client is None or not client.is_connected:
            raise SoundbankError('TTS workers are unavailable', status=503)
        rpc = SimpleNamespace(client=client, core_id=self.store.bootstrap['node_id'], stopping=False,
                              calls=set(), streams=self.streams)
        request = encode({'protocol': wire.PROTOCOL, 'op': 'status', 'core_id': rpc.core_id,
                          'revision': revision, 'fingerprint': '0' * 64}, wire.MAX_REQUEST)
        async def probe(node):
            try:
                response = await client.request(wire.subject(node), request, timeout=1)
                header, pcm = wire.unpack(response.data)
                if (pcm or set(header) != {'protocol', 'worker_id', 'revision', 'fingerprint', 'state'}
                        or header['protocol'] != wire.PROTOCOL or header['worker_id'] != node
                        or type(header['revision']) is not int or header['revision'] != revision
                        or header['state'] not in {'idle', 'busy'}):
                    return None
                # Reuse the request validator for the fingerprint's exact format.
                wire.request(encode({'protocol': wire.PROTOCOL, 'op': 'status', 'core_id': rpc.core_id,
                    'revision': revision, 'fingerprint': header['fingerprint']}, wire.MAX_REQUEST))
                return header['fingerprint']
            except Exception:
                return None
        identities = await asyncio.gather(*(probe(node) for node in self.nodes))
        ready = [(node, identity) for node, identity in zip(self.nodes, identities) if identity]
        if not ready:
            raise SoundbankError('Save and Apply runtime before generating with the selected TTS', status=409)
        if len({identity for _, identity in ready}) != 1:
            raise SoundbankError('TTS workers have different voices; Apply runtime first', status=409)
        pool = TTSPool(rpc, {'revision': revision, 'fingerprint': ready[0][1],
                             'workers': [node for node, _ in ready]})
        try:
            return await pool.generate(0, text)
        except WorkerRpcError as error:
            raise SoundbankError('TTS generation unavailable; retry when a worker is free', status=503) from error

    def _tokens(self, entry, config):
        audio = AudioCompiler(config).runtime_audio_contract()
        result = {}
        for asset in (entry, entry.get('optimized')):
            if asset is None:
                continue
            payload = {'asset': {'file': asset['file'], 'cloud': asset['cloud']},
                       'audio': audio, 'authority': self.authority, 'expires': int(time.time()) + 86400}
            if asset['file'].endswith('.p3'):
                payload['asset'].update(audio)
            raw = canonical_bytes(payload)
            signature = hmac.new(self.key, b'soundbank-preview-v1\0' + raw, hashlib.sha256).hexdigest()
            result[asset['file']] = base64.urlsafe_b64encode(raw).decode() + '.' + signature
        return result

    def _lookup(self, filename, config, token=None):
        if token:
            if not self.key or not isinstance(token, str) or len(token) > 4096:
                raise SoundbankError('Invalid draft preview', status=400)
            try:
                body, signature = token.split('.')
                raw = base64.b64decode(body, altchars=b'-_', validate=True)
                expected = hmac.new(self.key, b'soundbank-preview-v1\0' + raw, hashlib.sha256).hexdigest()
                if not hmac.compare_digest(signature, expected):
                    raise ValueError
                value = json.loads(raw)
                if (set(value) != {'asset', 'audio', 'authority', 'expires'}
                        or value['authority'] != self.authority or type(value['expires']) is not int
                        or not time.time() < value['expires'] <= time.time() + 86405
                        or value['asset']['file'] != filename):
                    raise ValueError
                config = copy.deepcopy(config)
                config.setdefault('xiaozhi', {})['audio_params'] = {'sample_rate': value['audio']['sample_rate']}
                asset = value['asset']
                test = copy.deepcopy(config)
                test['static_soundbank']['entries'] = {'Preview': asset}
                validate_soundbank_cloud_metadata(test)
                return asset, config
            except (ValueError, TypeError, KeyError):
                raise SoundbankError('Draft preview expired or invalid; sync Settings', status=400) from None
        for _, name, _, metadata, _ in _assets(config):
            if name == filename and metadata.get('cloud'):
                return copy.deepcopy(metadata), config
        raise SoundbankError('Soundbank audio is not in saved entries or this draft', status=404)

    def _obtain(self, filename, config, token=None):
        metadata, config = self._lookup(filename, config, token)
        test = copy.deepcopy(config)
        # Treat a standalone optimized packet stream with its pinned sample rate.
        rate = metadata.get('sample_rate', config['xiaozhi']['audio_params']['sample_rate'])
        test.setdefault('xiaozhi', {})['audio_params'] = {'sample_rate': rate}
        test['static_soundbank']['entries'] = {'Asset': metadata}
        # _assets already resolves the configured root and validates the file name.
        root, name, path, metadata, optimized = next(_assets(test))
        if metadata['cloud']['size'] > MAX_ASSET_BYTES:
            raise SoundbankError('Soundbank audio exceeds the preview limit', status=413)
        content = self.assets._obtain(root, name, path, metadata, optimized, test)
        return content, test

    def preview(self, filename, token=None):
        _, config = self.snapshot()
        content, config = self._obtain(filename, config, token)
        if filename.endswith('.p3'):
            import opuslib_next
            rate = AudioCompiler(config)._sample_rate()
            packets = p3.load_validated_opus_bytes(content, sample_rate=rate)
            decoder = opuslib_next.Decoder(rate, 1)
            samples = rate * p3.P3_FRAME_DURATION_MS // 1000
            output = io.BytesIO()
            with wave.open(output, 'wb') as wav:
                wav.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
                for packet in packets:
                    wav.writeframesraw(decoder.decode(packet, samples))
            return output.getvalue(), 'audio/wav'
        return content, 'audio/wav' if filename.endswith('.wav') else 'audio/mpeg'

    def _publish(self, config, entry, directory):
        staged = copy.deepcopy(config)
        staged['static_soundbank'] = {'directory': str(directory), 'entries': {'Draft': entry}}
        published = self.assets.publish(staged, {})['static_soundbank']['entries']['Draft']
        return published

    def _finish_generation(self, revision, config, title, text, result):
        self.snapshot(revision)
        if not result.pcm or len(result.pcm) > wire.MAX_PCM or len(result.pcm) % 2 or result.sample_rate not in wire.RATES:
            raise SoundbankError('TTS returned invalid audio', status=502)
        self.workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        _safe_target(self.workspace, 'check.wav')
        with tempfile.TemporaryDirectory(dir=self.workspace) as directory:
            root = Path(directory)
            source = root / 'source.wav'
            with wave.open(str(source), 'wb') as wav:
                wav.setparams((1, 2, result.sample_rate, 0, 'NONE', 'not compressed'))
                wav.writeframes(result.pcm)
            compiler = AudioCompiler(config)
            provider, options = compiler._generation_config()
            rate = compiler._sample_rate()
            name = compiler._generated_filename(title, text, provider,
                generation_fingerprint(provider, sanitize_generation_settings(options)), compiler._audio_contract(rate))
            canonical, optimized = root / name, root / Path(name).with_suffix('.p3')
            compiler._normalize_wav(source, canonical, rate)
            compiler._compile_p3(canonical, optimized, rate)
            entry = {'file': name, 'text': text, 'optimized': {'file': optimized.name,
                **compiler.runtime_audio_contract()}, 'generated_by': {'provider': provider,
                'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}}
            entry = self._publish(config, entry, root)
            return {'success': True, 'entry': entry, 'previews': self._tokens(entry, config)}

    async def generate(self, text, title=None, revision=None):
        if not self.enabled or self.stopping:
            raise SoundbankError('Cluster Soundbank authoring is unavailable', status=503)
        if self.lock.locked():
            raise SoundbankError('Soundbank authoring is busy; retry after it finishes', status=409)
        text = AudioCompiler._validate_text(text)
        title = AudioCompiler._validate_title(title or text)
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            async with self.lock:
                current, config = await self.reconciliation.operation(self.snapshot, revision)
                result = await self.generate_pcm(current, text)
                return await self.reconciliation.operation(self._finish_generation, current, config, title, text, result)
        finally:
            self.tasks.discard(task)

    def _optimize(self, filename, token, revision):
        _, config = self.snapshot(revision)
        metadata, _ = self._lookup(filename, config, token)
        content, audio_config = self._obtain(filename, config, token)
        self.workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        _safe_target(self.workspace, 'check.wav')
        with tempfile.TemporaryDirectory(dir=self.workspace) as directory:
            root = Path(directory)
            target = _safe_target(root, filename, create=True)
            target.write_bytes(content)
            staged = copy.deepcopy(audio_config)
            staged['static_soundbank']['directory'] = str(root)
            optimized = AudioCompiler(staged).optimize(filename)
            entry = self._publish(staged, {'file': filename, 'cloud': metadata['cloud'], 'optimized': optimized}, root)
            return {'success': True, 'optimized': entry['optimized'], 'previews': self._tokens(entry, audio_config)}

    async def optimize(self, filename, token=None, revision=None):
        if not self.enabled or self.stopping:
            raise SoundbankError('Cluster Soundbank authoring is unavailable', status=503)
        if self.lock.locked():
            raise SoundbankError('Soundbank authoring is busy; retry after it finishes', status=409)
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            async with self.lock:
                return await self.reconciliation.operation(self._optimize, filename, token, revision)
        finally:
            self.tasks.discard(task)
