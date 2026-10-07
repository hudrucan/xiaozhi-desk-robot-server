"""Atomic read-only secret snapshot for the restricted local runtime installer."""
import hashlib
import json
import os
import stat
from pathlib import Path

from config.cloud_secrets import REFERENCE, SecretProvider, MissingSecret

MAX_BYTES = 4 * 1024 * 1024


class CacheSecrets(SecretProvider):
    def __init__(self, node_id, directory, owner_uid):
        self.node_id, self.owner_uid = node_id, owner_uid
        self.path = Path(directory) / (hashlib.sha256(node_id.encode()).hexdigest() + '.json')
        self.values = None

    def get(self, name):
        if self.values is None:
            if any(path.is_symlink() for path in (self.path, *self.path.parents)):
                raise ValueError('Invalid private secret snapshot')
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, self.owner_uid}
                        or info.st_mode & 0o027 or info.st_size > MAX_BYTES):
                    raise ValueError('Invalid private secret snapshot')
                def unique(pairs):
                    value = dict(pairs)
                    if len(value) != len(pairs):
                        raise ValueError('Duplicate secret snapshot field')
                    return value
                value = json.loads(stream.read(MAX_BYTES + 1), object_pairs_hook=unique)
            if (not isinstance(value, dict) or set(value) != {'node_id', 'values'}
                    or value['node_id'] != self.node_id or not isinstance(value['values'], dict)
                    or any(not isinstance(key, str) or not REFERENCE.fullmatch('${secret:' + key + '}')
                           or not isinstance(item, str) for key, item in value['values'].items())):
                raise ValueError('Invalid private secret snapshot')
            # LocalSecretStore publishes atomically and never overwrites a named
            # reference. A single immutable read needs no write lock or chmod.
            self.values = value['values']
        if name not in self.values:
            raise MissingSecret('Required node-local secret is unavailable')
        return self.values[name]

    def put_many(self, values):
        raise ValueError('Runtime installer cannot write the credential dataset')
