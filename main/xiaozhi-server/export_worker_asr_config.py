"""Cache-only selected ASR/VAD export; no Drive, audio or provider runtime I/O."""
import argparse
import copy
import hashlib
import json
import logging
from pathlib import Path

from core.cluster.asr_config import validate_bundle
from export_worker_llm_config import write_private


def build_bundle(store, model_path, sha256, expected_revision, source_root=None):
    from config.cloud_layers import shared_cluster
    from config.config_loader import merge_configs
    snapshot = store._read_cache('desired.json')
    revision = snapshot['payload']['manifest']['revision']
    if revision != expected_revision or not shared_cluster(snapshot['payload']['object']):
        raise ValueError('ASR export requires matching shared V2 revision')
    defaults, overrides = store._resolve(snapshot['payload']['object'])
    config = merge_configs(defaults, overrides)
    selected = config.get('selected_module', {})
    source = copy.deepcopy(config.get('ASR', {}).get(selected.get('ASR'), {}))
    if source.get('http_proxy') or source.get('https_proxy'):
        raise ValueError('ASR worker proxy configuration is unsupported')
    if source.get('type') == 'sherpa_streaming':
        if source_root is None or source.get('provider', 'cpu') != 'cpu' or source.get('decoding_method', 'greedy_search') != 'greedy_search':
            raise ValueError('Local ASR requires explicit source root and CPU greedy decoding')
        root = Path(source_root).resolve()
        directory = (root / source['model_dir']).resolve()
        if not directory.is_relative_to(root):
            raise ValueError('Local ASR assets must belong to the provisioned source root')
        models = {}
        for key in ('encoder','decoder','joiner','tokens'):
            path = directory / source[key]
            if path.is_symlink() or not path.resolve().is_relative_to(directory) or not path.is_file():
                raise ValueError('Required local ASR asset is missing')
            with path.open('rb') as stream:
                checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
            models[key] = {'path': str(path), 'sha256': checksum}
        provider = {'type':'sherpa_streaming', 'models':models,
            'num_threads':source.get('num_threads',2), 'sentence_case':source.get('sentence_case',True),
            'final_padding_ms':source.get('final_padding_ms',0)}
    else:
        provider = {key: source.get(key, default) for key, default in
            [('type', None), ('model_name', None), ('api_key', None), ('language', 'auto'), ('mode', 'VERBATIM')]}
        provider = store.secrets.resolve({'ASR': provider})['ASR']
    vad_source = config.get('VAD', {}).get(selected.get('VAD'), {})
    if vad_source.get('type') != 'silero':
        raise ValueError('ASR worker currently requires selected Silero VAD')
    vad = {'model_path': str(model_path), 'sha256': sha256,
           'threshold': vad_source.get('threshold', 0.5),
           'threshold_low': vad_source.get('threshold_low', 0.3),
           'min_silence_duration_ms': vad_source.get('min_silence_duration_ms', 200)}
    value = validate_bundle({'protocol': 'xiaozhi-worker-asr-config-v1',
        'worker_id': store.bootstrap['node_id'], 'revision': revision, 'provider': provider, 'vad': vad},
        store.bootstrap['node_id'])
    path = Path(model_path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError('VAD model must be a local regular file')
    if hashlib.sha256(path.read_bytes()).hexdigest() != sha256:
        raise ValueError('VAD checksum differs')
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--source-root', required=True)
    parser.add_argument('--expected-revision', required=True, type=int)
    parser.add_argument('--vad-model', required=True)
    parser.add_argument('--vad-sha256', required=True)
    args = parser.parse_args()
    try:
        from config.bootstrap import load_bootstrap
        from config.google_drive_config import GoogleDriveConfigStore
        from config.cloud_secrets import LocalSecretStore
        root = Path(args.source_root).resolve()
        bootstrap = load_bootstrap(root / 'data/bootstrap.yaml')
        store = GoogleDriveConfigStore(bootstrap, transport=object(), default_path=str(root / 'config.yaml'),
            cache_dir=root / 'data/cloud-config',
            secret_provider=LocalSecretStore(bootstrap['node_id'], root / 'data/node-secrets'))
        value = build_bundle(store, args.vad_model, args.vad_sha256, args.expected_revision, root)
        changed = write_private(args.output, value)
        print(json.dumps({'changed': changed, 'revision': value['revision'], 'worker_id': value['worker_id'],
                          'adapter': value['provider']['type']}))
        return 0
    except Exception:
        logging.error('ASR export unavailable; check selected ASR/VAD, local model, credentials and revision')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
