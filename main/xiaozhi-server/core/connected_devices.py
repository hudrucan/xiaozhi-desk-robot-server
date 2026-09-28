"""Process-local registry for authenticated device WebSocket connections."""

import asyncio
from typing import Any


class ConnectedDeviceRegistry:
    """Map one device ID to its newest live connection handler."""

    def __init__(self):
        self._handlers = {}
        self._lock = asyncio.Lock()

    async def register(self, device_id: str, handler):
        if not device_id:
            return None
        async with self._lock:
            previous = self._handlers.get(device_id)
            self._handlers[device_id] = handler
            return previous if previous is not handler else None

    async def unregister(self, device_id: str, handler) -> bool:
        """Remove only an entry that still belongs to this exact handler."""
        if not device_id:
            return False
        async with self._lock:
            if self._handlers.get(device_id) is not handler:
                return False
            del self._handlers[device_id]
            return True

    async def get(self, device_id: str):
        async with self._lock:
            return self._handlers.get(device_id)

    async def send(self, device_id: str, payload: dict[str, Any]) -> bool:
        handler = await self.get(device_id)
        if handler is None:
            return False
        return await handler.send_json(payload)


connected_devices = ConnectedDeviceRegistry()


async def register_connected_device_after_hello(
    device_id: str,
    handler,
    registry: ConnectedDeviceRegistry = connected_devices,
):
    """Publish a handler only after its protocol hello has completed."""
    previous = await registry.register(device_id, handler)
    if previous is not None:
        asyncio.create_task(previous.close())
    return previous


async def get_connected_device(device_id: str):
    return await connected_devices.get(device_id)


async def send_to_connected_device(device_id: str, payload: dict[str, Any]) -> bool:
    """Narrow internal primitive for future server-initiated device events."""
    return await connected_devices.send(device_id, payload)
