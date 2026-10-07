"""Copy pinned verified audio into the core's private immutable cache, offline."""
import argparse
import copy
import grp
import json
import logging
import os
import shutil
import tempfile
import sys
from pathlib import Path

from core.cluster.llm_protocol import decode
from core.cluster.tts_config import MAX_BUNDLE, load_bundle, validate_bundle
from core.cluster.tts_soundbank import SoundbankPlayback, assets, read_asset, validate
from export_worker_llm_config import write_private


def provision(bundle, source_cache, destination, group=None):
    value = copy.deepcopy(bundle)
    if 'soundbank' not in value:
        return value, False
    bank = validate(value['soundbank'], value['revision'])
    source, destination = Path(source_cache), Path(destination)
    if (not source.is_absolute() or str(source) != bank['root']
            or not destination.is_absolute()
            or any(part.is_symlink() for part in (destination, *destination.parents))):
        raise ValueError('Invalid private Soundbank provisioning paths')
    target = destination / bank['fingerprint']
    bank['root'] = str(target)
    changed = False
    if not target.exists():
        # The role creates only this assets-only directory with root:core 0750.
        # Never grant core access to the control-plane credential/cache group.
        stage = Path(tempfile.mkdtemp(prefix='.soundbank-', dir=destination))
        try:
            for item in assets(bank):
                content = read_asset(source, item)
                path = stage / item['name']
                with path.open('wb') as stream:
                    os.fchmod(stream.fileno(), 0o640 if group is not None else 0o600)
                    if group is not None:
                        os.fchown(stream.fileno(), -1, group)
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
            staged = {**bank, 'root': str(stage)}
            SoundbankPlayback(staged).verify()
            if group is not None:
                os.chown(stage, -1, group)
                os.chmod(stage, 0o750)
            if target.exists() or target.is_symlink():
                raise ValueError('Soundbank destination appeared during staging')
            os.rename(stage, target)
            changed = True
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    # Existing immutable caches are verified, never overwritten in place.
    SoundbankPlayback(bank).verify()
    return value, changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True)
    parser.add_argument('--node-id', required=True)
    parser.add_argument('--source-cache', required=True)
    parser.add_argument('--destination', required=True)
    parser.add_argument('--group', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        value = validate_bundle(decode(sys.stdin.buffer.read(MAX_BUNDLE + 1), MAX_BUNDLE), args.node_id) \
            if args.bundle == '-' else load_bundle(args.bundle, args.node_id)
        value, copied = provision(value, args.source_cache, args.destination, grp.getgrnam(args.group).gr_gid)
        changed = write_private(args.output, value, max_bytes=MAX_BUNDLE)
        print(json.dumps({'changed': changed or copied, 'revision': value['revision']}))
        return 0
    except Exception:
        logging.error('Core Soundbank provisioning unavailable; check pinned revision, cache and audio contracts')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
