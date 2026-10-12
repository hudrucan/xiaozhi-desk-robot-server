"""Bounded, boot-local admission flag written only by the node controller."""
import json
import os
import stat
import time
from pathlib import Path

PATH = Path('/run/desk-node-maintenance/state.json')


def requested(node_id):
    try:
        fd = os.open(PATH, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                return False
            raw = stream.read(2049)
        if len(raw) > 2048:
            return False
        value = json.loads(raw)
        return (value.get('protocol') == 'desk-node-maintenance-v1'
                and value.get('node') == node_id
                and value.get('boot_id') == Path('/proc/sys/kernel/random/boot_id').read_text().strip()
                and type(value.get('expires')) is int
                and time.time() < value['expires'] <= time.time() + 660)
    except (OSError, ValueError, TypeError, AttributeError):
        return False


class Admission:
    """Unsubscribe only job-open queues; keep active job input/result paths alive."""
    def __init__(self, client, activity, node_id):
        self.client, self.activity, self.node_id = client, activity, node_id
        self.bindings = []
        self.paused = False
        self.dirty = False
        self.callbacks = set()

    async def subscribe(self, subject, **kwargs):
        import asyncio
        callback = kwargs['cb']
        async def guarded(message):
            # NATS unsubscribe cancels the callback consumer. Keep an in-flight
            # admission ACK alive; its provider task has separate ownership.
            task = asyncio.create_task(callback(message))
            self.callbacks.add(task)
            def finished(done):
                self.callbacks.discard(done)
                if not done.cancelled():
                    done.exception()
            task.add_done_callback(finished)
            await asyncio.shield(task)
        kwargs = {**kwargs, 'cb': guarded}
        subscription = await self.client.subscribe(subject, **kwargs)
        self.bindings.append([subject, kwargs, subscription])

    async def close(self):
        import asyncio
        for task in tuple(self.callbacks):
            task.cancel()
        await asyncio.gather(*self.callbacks, return_exceptions=True)

    async def run(self):
        import asyncio
        while True:
            want = await asyncio.to_thread(requested, self.node_id)
            if (want != self.paused or self.dirty) and self.client.is_connected:
                # Serialized with all admission callbacks on this event loop.
                self.activity.draining = want
                try:
                    if want:
                        for binding in self.bindings:
                            if binding[2] is not None:
                                from nats.errors import BadSubscriptionError
                                try:
                                    await binding[2].unsubscribe()
                                except BadSubscriptionError:
                                    pass  # Already removed before a failed flush.
                                binding[2] = None
                    else:
                        for binding in self.bindings:
                            if binding[2] is None:
                                binding[2] = await self.client.subscribe(binding[0], **binding[1])
                    await self.client.flush(timeout=2)
                    if want:
                        await asyncio.gather(*tuple(self.callbacks), return_exceptions=True)
                    self.paused = want
                    self.activity.admission_paused = want
                    self.dirty = False
                except Exception:
                    # Retry after transient NATS loss; never acknowledge pause
                    # before all queues have been removed and flushed.
                    self.activity.admission_paused = False
                    self.dirty = True
            await asyncio.sleep(.5)
