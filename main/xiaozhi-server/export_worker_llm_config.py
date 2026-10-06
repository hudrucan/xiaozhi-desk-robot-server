"""Export only the selected LLM from a validated local Cloud cache; no Drive I/O."""
import argparse
import copy
import logging
import os
import tempfile
from pathlib import Path

from core.cluster.llm_config import FIELDS, validate_bundle


def build_bundle(store):
    # Use the existing Cloud cache validator, source identity and layer resolver.
    # No refresh, publication, Soundbank materialization or active mark occurs.
    from config.cloud_layers import shared_cluster
    from config.config_loader import merge_configs
    snapshot = store._read_cache('desired.json')
    if not shared_cluster(snapshot['payload']['object']):
        raise ValueError('Text worker requires explicit shared V2 Cloud configuration')
    defaults, overrides = store._resolve(snapshot['payload']['object'])
    config = merge_configs(defaults, overrides)
    selected = config.get('selected_module', {}).get('LLM')
    provider = copy.deepcopy(config.get('LLM', {}).get(selected, {}))
    if provider.get('http_proxy') or provider.get('https_proxy'):
        raise ValueError('Proxy-enabled worker deployment needs a separate bounded adapter')
    # Resolve only LLM credentials: missing unrelated ASR/TTS secrets must not
    # force initialization or prevent this text-only phase.
    provider = {key: value for key, value in provider.items() if key in FIELDS}
    provider = store.secrets.resolve({'LLM': provider})['LLM']
    timeout = provider.get('timeout', 120)
    if type(timeout) not in (int, float) or not timeout > 0:
        raise ValueError('Invalid LLM timeout')
    provider['timeout'] = min(timeout, 30)  # Explicit RPC phase budget, not Cloud mutation.
    value = {'protocol': 'xiaozhi-worker-llm-config-v1', 'worker_id': store.bootstrap['node_id'],
             'revision': snapshot['payload']['manifest']['revision'],
             'prompt': config.get('prompt', ''), 'provider': provider}
    return validate_bundle(value, store.bootstrap['node_id'])


def write_private(path, value):
    from core.cluster.llm_protocol import encode
    from core.cluster.llm_config import MAX_CONFIG_BYTES
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.parent.is_dir():
        raise ValueError('Export requires an existing private destination directory')
    descriptor, temporary = tempfile.mkstemp(prefix='.worker-llm-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(encode(value, MAX_CONFIG_BYTES))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        from config.bootstrap import load_bootstrap
        from config.google_drive_config import GoogleDriveConfigStore
        bootstrap = load_bootstrap()
        # A sentinel transport makes accidental live use fail; export is cache-only.
        store = GoogleDriveConfigStore(bootstrap, transport=object())
        value = build_bundle(store)
        write_private(args.output, value)
        print('Worker LLM configuration exported privately; revision=' + str(value['revision']))
        return 0
    except Exception:
        logging.error('Worker LLM export unavailable; validate shared cache, selected provider and local secrets')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
