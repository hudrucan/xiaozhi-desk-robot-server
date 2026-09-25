import asyncio
import ipaddress

from aiohttp import web

from core.api.base_handler import BaseHandler


class MemoryHandler(BaseHandler):
    """Expose the active explicit local-memory provider to the settings UI."""

    def __init__(self, config, memory_provider):
        super().__init__(config)
        self.memory_provider = memory_provider
        settings_config = config.get("server", {}).get("settings", {})
        self.allow_remote = bool(settings_config.get("allow_remote", False))

    @staticmethod
    def _disable_cache(response):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response

    def _require_access(self, request):
        if self.allow_remote:
            return
        peer = (
            request.transport.get_extra_info("peername")
            if request.transport
            else None
        )
        host = peer[0] if peer else ""
        try:
            address = ipaddress.ip_address(host)
            mapped_address = getattr(address, "ipv4_mapped", None)
            if address.is_loopback or (
                mapped_address is not None and mapped_address.is_loopback
            ):
                return
        except ValueError:
            pass
        raise web.HTTPForbidden(
            text="Memory settings only accept local requests. Set "
            "server.settings.allow_remote in data/.config.yaml to enable LAN access."
        )

    def _supported_provider(self):
        provider = self.memory_provider
        selected = self.config.get("selected_module", {}).get("Memory")
        provider_config = self.config.get("Memory", {}).get(selected, {})
        provider_type = provider_config.get("type", selected)
        if provider_type != "mem_local_explicit":
            return None
        required_methods = (
            "delete_entry",
            "inspect_entries",
            "remember",
            "update_entry",
        )
        if provider is None or not all(
            callable(getattr(provider, method, None)) for method in required_methods
        ):
            return None
        return provider

    async def _snapshot(self):
        provider = self._supported_provider()
        selected = self.config.get("selected_module", {}).get("Memory")
        if provider is None:
            return {
                "available": False,
                "selected_provider": selected,
                "reason": (
                    "Select the explicit local-memory provider to inspect "
                    "memories."
                ),
                "entries": [],
            }
        if not getattr(provider, "role_id", None):
            select_stored_scope = getattr(provider, "select_stored_scope", None)
            if callable(select_stored_scope):
                await asyncio.to_thread(select_stored_scope)
        snapshot = await asyncio.to_thread(provider.inspect_entries)
        snapshot["available"] = True
        snapshot["selected_provider"] = selected
        return snapshot

    async def handle_get(self, request):
        self._require_access(request)
        return self._disable_cache(web.json_response(await self._snapshot()))

    async def handle_post(self, request):
        self._require_access(request)
        provider = await self._require_initialized_provider()
        body = await self._read_json(request)
        content = str(body.get("content", "")).strip()
        if not content:
            raise web.HTTPBadRequest(text="Memory content cannot be empty")
        try:
            remembered = await asyncio.to_thread(
                provider.remember, content, **self._metadata(body)
            )
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error)) from error
        if not remembered:
            raise web.HTTPBadRequest(text="Memory content cannot be empty")
        return self._disable_cache(web.json_response(await self._snapshot()))

    async def handle_put(self, request):
        self._require_access(request)
        provider = await self._require_initialized_provider()
        body = await self._read_json(request)
        content = str(body.get("content", "")).strip()
        if not content:
            raise web.HTTPBadRequest(text="Memory content cannot be empty")
        try:
            updated = await asyncio.to_thread(
                provider.update_entry,
                request.match_info["entry_id"],
                content,
                **self._metadata(body),
            )
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error)) from error
        if not updated:
            raise web.HTTPNotFound(text="Memory entry was not found")
        return self._disable_cache(web.json_response(await self._snapshot()))

    async def handle_delete(self, request):
        self._require_access(request)
        provider = await self._require_initialized_provider()
        deleted = await asyncio.to_thread(
            provider.delete_entry,
            request.match_info["entry_id"],
        )
        if not deleted:
            raise web.HTTPNotFound(text="Memory entry was not found")
        return self._disable_cache(web.json_response(await self._snapshot()))

    async def _require_initialized_provider(self):
        provider = self._supported_provider()
        if provider is None:
            raise web.HTTPConflict(
                text="The active memory provider is not editable."
            )
        snapshot = await asyncio.to_thread(provider.inspect_entries)
        if not snapshot.get("initialized"):
            raise web.HTTPConflict(
                text="Connect the robot once before editing its memory."
            )
        return provider

    @staticmethod
    async def _read_json(request):
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="Expected application/json")
        try:
            body = await request.json()
        except (ValueError, TypeError) as error:
            raise web.HTTPBadRequest(text="Invalid JSON body") from error
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="Request body must be an object")
        return body

    @staticmethod
    def _metadata(body):
        fields = (
            "type",
            "project",
            "entities",
            "tags",
            "importance",
            "pinned",
            "active",
            "supersedes",
        )
        return {field: body[field] for field in fields if field in body}
