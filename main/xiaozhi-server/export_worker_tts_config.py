"""Export matching segment TTS bundles from validated local Cloud cache only."""
import argparse
import copy
import hashlib
import json
import logging
import os
import stat
from pathlib import Path

from core.cluster.tts_config import DEFAULTS, MAX_BUNDLE, PROTOCOL, fingerprint, relative_path, validate_bundle
from export_worker_llm_config import write_private


def export_soundbank(store, config, revision):
    from config.config_store import canonical_bytes, checksum
    from core.soundbank import (soundbank_entry_filename, soundbank_entry_optimized,
                               soundbank_entry_text, soundbank_p3_sample_rate,
                               validate_soundbank_cloud_metadata)
    from core.cluster.tts_soundbank import PROTOCOL as CACHE_PROTOCOL, fingerprint as cache_fingerprint, read_asset, validate
    subset = {'static_soundbank': copy.deepcopy(config.get('static_soundbank', {})),
              'xiaozhi': {'audio_params': copy.deepcopy(config.get('xiaozhi', {}).get('audio_params', {}))}}
    validate_soundbank_cloud_metadata(config)
    if getattr(store, 'soundbank_assets', None) is None:
        raise ValueError('Soundbank requires a complete verified local cache')
    cache = Path(store.soundbank_assets.cache_dir)
    descriptor = os.open(cache.parent / 'ready.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o027 or info.st_size > 2 * 1024 * 1024:
            raise ValueError('Soundbank requires a private bounded ready index')
        index = json.loads(stream.read(2 * 1024 * 1024 + 1))
    if (index.get('protocol') != 'xiaozhi-soundbank-cache-v1'
            or index.get('node_id') != store.bootstrap['node_id']
            or type(index.get('revision')) is not int or index['revision'] != revision
            or index.get('configuration') != subset
            or index.get('fingerprint') != checksum(canonical_bytes(subset))):
        raise ValueError('Soundbank cache is not ready for the selected desired revision')

    def asset(metadata, optimized):
        pointer = metadata['cloud']  # Enabled V2 entries must be published.
        suffix = Path(soundbank_entry_filename(metadata)).suffix.lower()
        return {'name': pointer['sha256'] + suffix, 'sha256': pointer['sha256'],
                'size': pointer['size'], 'sample_rate': soundbank_p3_sample_rate(
                    config, metadata if optimized else None) if suffix == '.p3' else None}

    entries = []
    for phrase, entry in subset['static_soundbank'].get('entries', {}).items():
        optimized = soundbank_entry_optimized(entry)
        entries.append({'phrase': phrase, 'text': soundbank_entry_text(entry),
                        'canonical': asset(entry, False),
                        'optimized': asset(optimized, True) if optimized is not None else None})
    value = validate({'protocol': CACHE_PROTOCOL, 'revision': revision, 'root': str(cache),
                      'entries': entries, 'fingerprint': cache_fingerprint(entries)}, revision)
    from core.cluster.tts_soundbank import assets
    for item in assets(value):
        read_asset(cache, item)
    return value


def build_bundle(store, source_root, expected_revision, workers):
    from config.cloud_layers import shared_cluster
    from config.config_loader import merge_configs
    snapshot = store._read_cache('desired.json')
    revision = snapshot['payload']['manifest']['revision']
    if revision != expected_revision or not shared_cluster(snapshot['payload']['object']):
        raise ValueError('TTS export requires matching validated shared V2 cache')
    config = merge_configs(*store._resolve(snapshot['payload']['object']))
    source = config.get('TTS', {}).get(config.get('selected_module', {}).get('TTS'), {})
    if source.get('type') != 'sherpa':
        raise ValueError('Segment worker requires the selected Sherpa TTS provider')
    soundbank = export_soundbank(store, config, revision) if config.get('static_soundbank', {}).get('enabled') else None
    options = {key: copy.deepcopy(source.get(key, default)) for key, default in DEFAULTS.items()}
    for key in ('model', 'tokens', 'data_dir'):
        relative_path(options[key])
    root = Path(source_root).resolve()
    directory = root / source['model_dir']
    if directory.is_symlink() or not directory.resolve().is_relative_to(root):
        raise ValueError('TTS model must belong to the provisioned source root')
    directory = directory.resolve()
    paths = [directory / options['model'], directory / options['tokens']]
    sidecar = directory / (options['model'] + '.json')
    if sidecar.exists():
        paths.append(sidecar)
    paths.extend(p for p in (directory / options['data_dir']).rglob('*') if p.is_file())
    files = {}
    for path in paths:
        if (path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(directory)
                or path.stat().st_size > 512 * 1024 * 1024
                or len(files) >= 2048
                or any(p.is_symlink() for p in path.parents if p.is_relative_to(directory))):
            raise ValueError('Required local TTS asset is unavailable')
        with path.open('rb') as stream:
            files[path.relative_to(directory).as_posix()] = hashlib.file_digest(stream, 'sha256').hexdigest()
    node = store.bootstrap['node_id']
    value = {'protocol': PROTOCOL, 'worker_id': node, 'revision': revision,
        'workers': workers, 'model_root': str(directory), 'options': options,
        'files': files, 'fingerprint': fingerprint(options, files)}
    from core.cluster.tts_config import DEFAULT_ERROR_RESPONSE
    value['system_error_response'] = config.get('system_error_response', DEFAULT_ERROR_RESPONSE)
    from core.cluster.session_config import export_config
    value['session'] = export_config(config)
    from core.cluster.vision_config import export_config as export_vision
    value['vision_enabled'] = export_vision(config, store.secrets) is not None
    from core.cluster.memory_protocol import export_config as export_memory
    memory = export_memory(config)
    if memory is not None:
        value['memory'] = memory
    if soundbank is not None:
        value['soundbank'] = soundbank
    return validate_bundle(value, node)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', required=True)
    parser.add_argument('--expected-revision', required=True, type=int)
    parser.add_argument('--workers', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        from config.bootstrap import load_bootstrap
        from config.google_drive_config import GoogleDriveConfigStore
        from config.cloud_secrets import LocalSecretStore
        root = Path(args.source_root).resolve()
        bootstrap = load_bootstrap(root / 'data/bootstrap.yaml')
        store = GoogleDriveConfigStore(bootstrap, transport=object(), default_path=str(root / 'config.yaml'),
            cache_dir=root / 'data/cloud-config', secret_provider=LocalSecretStore(bootstrap['node_id'], root / 'data/node-secrets'))
        value = build_bundle(store, root, args.expected_revision, args.workers.split(','))
        changed = write_private(args.output, value, max_bytes=MAX_BUNDLE)
        print(json.dumps({'changed': changed, 'revision': value['revision'], 'worker_id': value['worker_id']}))
        return 0
    except Exception:
        logging.error('TTS export unavailable; check selected provider, model assets, revision and Soundbank support')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
