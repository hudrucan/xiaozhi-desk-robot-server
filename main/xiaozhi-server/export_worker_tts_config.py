"""Export matching segment TTS bundles from validated local Cloud cache only."""
import argparse
import copy
import hashlib
import json
import logging
from pathlib import Path

from core.cluster.tts_config import DEFAULTS, MAX_BUNDLE, PROTOCOL, fingerprint, relative_path, validate_bundle
from export_worker_llm_config import write_private


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
    if config.get('static_soundbank', {}).get('enabled'):
        raise ValueError('Distributed Soundbank playback is not provisioned; do not silently synthesize cached phrases')
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
    return validate_bundle({'protocol': PROTOCOL, 'worker_id': node, 'revision': revision,
        'workers': workers, 'model_root': str(directory), 'options': options,
        'files': files, 'fingerprint': fingerprint(options, files)}, node)


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
