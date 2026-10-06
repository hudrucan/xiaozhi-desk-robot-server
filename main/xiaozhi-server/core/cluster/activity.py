"""Local worker activity only; no transcript, configuration or credentials."""
import asyncio
import json
import os
import time
import uuid
from pathlib import Path


class Activity:
    def __init__(self, path, worker_id):
        self.path = Path(path) if path else None
        if self.path and (not self.path.is_absolute() or self.path.name != 'activity.json'):
            raise ValueError('Invalid worker activity path')
        self.worker_id, self.instance = worker_id, uuid.uuid4().hex
        self.boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip() if self.path else ''
        self.counts = {'asr': 0, 'llm': 0}

    def change(self, kind, delta):
        self.counts[kind] += delta

    def write(self):
        if self.path:
            content = json.dumps({'protocol': 'xiaozhi-worker-activity-v1', 'worker_id': self.worker_id,
                'instance': self.instance, 'boot_id': self.boot, 'monotonic': time.monotonic(), **self.counts})
            temporary = self.path.with_suffix('.tmp')
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as stream:
                stream.write(content)
            os.replace(temporary, self.path)

    async def run(self):
        try:
            while True:
                await asyncio.to_thread(self.write)
                await asyncio.sleep(0.5)
        finally:
            if self.path:
                self.path.unlink(missing_ok=True)
