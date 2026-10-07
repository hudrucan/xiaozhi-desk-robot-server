"""Only desired-configuration Settings routes; no process-local runtime handlers."""

import asyncio
import copy
import json
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout, web

from config.config_loader import get_project_dir
from config.config_store import ConfigConflict, ConfigUnavailable
from core.api.settings_access import SettingsAccess
from core.cluster.config_reconciliation import CAPABILITIES, CONTROL_PROTOCOL, safe_source
from core.utils.config_editor import ConfigEditor
from core.cluster.soundbank_reconciliation import safe_status


class ControlPlaneSettingsHandler(SettingsAccess):
    access_error = "Control-plane Settings requires loopback or explicit XIAOZHI_CONTROL_PLANE_ALLOW_REMOTE=true"

    def __init__(self, reconciliation, secrets=None, runtime=None):
        self.reconciliation = reconciliation
        self.secrets = secrets
        self.runtime = runtime
        self.editor = ConfigEditor(reconciliation.store)
        self.allow_remote = reconciliation.config.allow_remote
        self.web_dir = str(Path(get_project_dir()) / "web/settings")

    def capabilities(self):
        capabilities = copy.deepcopy(CAPABILITIES)
        capabilities["secret_provisioning"] = self.secrets is not None
        capabilities["mqtt_bootstrap"] = self.reconciliation.config.bootstrap is not None
        capabilities["voice_diagnostics"] = True
        capabilities["runtime_apply"] = self.runtime is not None
        capabilities["memory"] = self.reconciliation.memory is not None
        return {"protocol": CONTROL_PROTOCOL, "capability_version": 1,
                "mode": "standalone", "capabilities": capabilities}

    def _decorate(self, payload):
        # Preserve the response's revision/config pair, rather than borrowing a
        # later concurrent status snapshot for the browser's next CAS base.
        payload["configuration_source"] = safe_source(payload["configuration_source"])
        payload["config_path"] = "Shared Cloud desired configuration"
        payload["control_plane"] = self.capabilities()
        return payload

    def _error(self, error):
        if isinstance(error, ConfigConflict):
            status, code, message = 409, "config_conflict", "Cloud configuration changed; sync and review before retrying"
        elif isinstance(error, (ConfigUnavailable, OSError)):
            status, code, message = 503, "cloud_unavailable", "Validated Cloud configuration is unavailable; retry synchronization"
        else:
            status, code, message = 400, "invalid_config", "Invalid desired configuration; shared plaintext secrets are unsupported and blank fields must preserve existing references"
        return self._disable_cache(web.json_response({
            "error": message, "code": code,
            "configuration_source": copy.deepcopy(self.reconciliation.source),
            "control_plane": self.capabilities(),
        }, status=status))

    async def handle_index(self, request):
        self._require_access(request)
        content = await asyncio.to_thread((Path(self.web_dir) / "index.html").read_text, encoding="utf-8")
        # Decide mode before app.js can schedule runtime-only status polling.
        content = content.replace("<body>", '<body data-settings-mode="control-plane">', 1)
        return self._disable_cache(web.Response(text=content, content_type="text/html"))

    async def handle_capabilities(self, request):
        self._require_access(request)
        return self._disable_cache(web.json_response(self.capabilities()))

    async def handle_get(self, request):
        self._require_access(request)
        try:
            payload = await self.reconciliation.operation(self.editor.read_public)
        except (OSError, ValueError, TypeError, KeyError) as error:
            return self._error(error)
        return self._disable_cache(web.json_response(self._decorate(payload)))

    async def handle_put(self, request):
        self._require_access(request)
        self._require_json(request)
        try:
            body = await request.json()
            if not isinstance(body, dict) or body.get("soundbank_retired_drafts"):
                raise ValueError
            payload = await self.reconciliation.operation(
                self.editor.update, body.get("config"), base_revision=body.get("base_revision"),
            )
        except (OSError, ValueError, TypeError, KeyError) as error:
            return self._error(error)
        return self._disable_cache(web.json_response(self._decorate(payload)))

    async def handle_sync(self, request):
        self._require_access(request)
        self._require_json(request)
        if not await self.reconciliation.reconcile():
            return self._error(ConfigUnavailable())
        return await self.handle_get(request)

    async def handle_migration(self, request):
        self._require_access(request)
        self._require_json(request)
        try:
            body = await request.json()
            if not isinstance(body, dict) or type(body.get("apply", False)) is not bool:
                raise ValueError
            result = await self.reconciliation.operation(
                self.editor.store.migrate_cluster, body.get("nodes"),
                base_revision=body.get("base_revision"), apply=body.get("apply", False),
            )
        except (OSError, ValueError, TypeError, KeyError) as error:
            return self._error(error)
        return self._disable_cache(web.json_response(result))

    async def handle_cluster(self, request):
        self._require_access(request)
        payload = self.reconciliation.status()
        payload["capabilities"]["secret_provisioning"] = self.secrets is not None
        payload["capabilities"]["mqtt_bootstrap"] = self.reconciliation.config.bootstrap is not None
        payload["capabilities"]["voice_diagnostics"] = True
        payload["capabilities"]["runtime_apply"] = self.runtime is not None
        payload["capabilities"]["memory"] = self.reconciliation.memory is not None
        return self._disable_cache(web.json_response(payload))

    async def handle_soundbank_cluster(self, request):
        """Read only configured private peers; no Drive I/O or user-supplied URLs."""
        self._require_access(request)
        local = self.reconciliation.status()
        peers = self.reconciliation.config.secrets
        nodes = peers.nodes if peers is not None else ((local["node_id"], None),)
        revision = local["configuration"].get("desired_revision")
        async with ClientSession(trust_env=False, timeout=ClientTimeout(total=3)) as session:
            async def one(node, endpoint):
                try:
                    if node == local["node_id"]:
                        payload = local
                    else:
                        async with session.get(endpoint + "/api/cluster", allow_redirects=False) as response:
                            if response.status != 200:
                                raise ValueError
                            data = bytearray()
                            async for chunk in response.content.iter_chunked(4096):
                                data.extend(chunk)
                                if len(data) > 32768:
                                    raise ValueError
                            payload = json.loads(data)
                    if payload.get("protocol") != CONTROL_PROTOCOL or payload.get("node_id") != node:
                        raise ValueError
                    result = safe_status(payload.get("soundbank"))
                    ready = (revision is not None and result["state"] in {"ready", "disabled"}
                             and result["desired_revision"] == result["synced_revision"] == revision
                             and result["expected_assets"] == result["verified_assets"])
                    return {"node_id": node, **result, "ready": ready}
                except Exception:
                    return {"node_id": node, "state": "unavailable", "ready": False}
            results = await asyncio.gather(*(one(node, endpoint) for node, endpoint in nodes))
        ready = sum(item["ready"] for item in results)
        return self._disable_cache(web.json_response({"protocol": "xiaozhi-soundbank-cluster-v1",
            "scope": "configured_peers" if peers is not None else "local_only",
            "desired_revision": revision, "ready_nodes": ready, "expected_nodes": len(nodes),
            "state": "ready" if ready == len(nodes) else "pending", "nodes": results}))

    async def handle_memory_cluster(self, request):
        self._require_access(request)
        local = self.reconciliation.status()
        authority = local.get("memory", {}).get("authority_fingerprint")
        peers = self.reconciliation.config.secrets
        nodes = peers.nodes if peers is not None else ((local["node_id"], None),)
        async with ClientSession(trust_env=False, timeout=ClientTimeout(total=3)) as session:
            async def one(node, endpoint):
                try:
                    if node == local["node_id"]:
                        payload = local
                    else:
                        async with session.get(endpoint + "/api/cluster", allow_redirects=False) as response:
                            if response.status != 200:
                                raise ValueError
                            data = bytearray()
                            async for chunk in response.content.iter_chunked(4096):
                                data.extend(chunk)
                                if len(data) > 32768:
                                    raise ValueError
                            payload = json.loads(data)
                    if payload.get("protocol") != CONTROL_PROTOCOL or payload.get("node_id") != node:
                        raise ValueError
                    memory = payload.get("memory", {})
                    revision = memory.get("memory_revision")
                    if type(revision) is not int or revision < 1:
                        raise ValueError
                    source_matches = (isinstance(authority, str) and len(authority) == 64
                                      and memory.get("authority_fingerprint") == authority)
                    ready = (source_matches and memory.get("state") == "ready" and memory.get("sync_state") == "synced"
                             and memory.get("write_mode") == "shared_cas" and memory.get("writable") is True)
                    return {"node_id": node, "memory_revision": revision, "ready": ready,
                            "authority_matches": source_matches}
                except Exception:
                    return {"node_id": node, "memory_revision": None, "ready": False, "authority_matches": False}
            results = await asyncio.gather(*(one(node, endpoint) for node, endpoint in nodes))
        revision = max((item["memory_revision"] or 0 for item in results), default=0) or None
        ready = sum(item["ready"] and item["memory_revision"] == revision for item in results)
        return self._disable_cache(web.json_response({"protocol": "xiaozhi-memory-cluster-v1",
            "memory_revision": revision, "ready_nodes": ready, "expected_nodes": len(nodes),
            "state": "ready" if ready == len(nodes) else "pending", "nodes": results}))

    async def handle_memory(self, request):
        self._require_access(request)
        service = self.reconciliation.memory
        if service is None:
            return self._disable_cache(web.json_response(
                {"error": "Shared Memory is unavailable", "code": "memory_unavailable"}, status=503))
        if service.backend is None:
            return self._disable_cache(web.json_response({"error":
                "Shared Memory is not ready. Select explicit Memory and provision the common Cloud Memory source on all nodes.",
                "code": "memory_unavailable"}, status=503))
        from core.memory_storage import MemoryConflict, MemoryReadOnly
        try:
            if set(request.query) - {"device_id"}:
                raise ValueError
            body = None
            if request.method != "GET":
                self._require_json(request)
                if request.content_length is not None and request.content_length > 16384:
                    raise web.HTTPRequestEntityTooLarge(max_size=16384, actual_size=request.content_length)
                data = bytearray()
                async for chunk in request.content.iter_chunked(4096):
                    data.extend(chunk)
                    if len(data) > 16384:
                        raise ValueError
                from core.cluster.memory_protocol import decode
                body = decode(bytes(data), 16384)
            entry_id = request.match_info.get("entry_id")
            if entry_id is not None:
                import re
                if not re.fullmatch("[a-zA-Z0-9_-]{1,128}", entry_id):
                    raise ValueError
            if request.method != "GET" and not request.query.get("device_id"):
                raise ValueError
            payload = await service.settings_operation(request.query.get("device_id"),
                request.method, body, entry_id)
            return self._disable_cache(web.json_response(payload))
        except web.HTTPException:
            raise
        except MemoryConflict:
            code, status, message = "memory_conflict", 409, "Memory changed. Refresh and review before retrying."
        except LookupError:
            code, status, message = "memory_not_found", 404, "Memory entry was not found."
        except (ValueError, TypeError, UnicodeError, RecursionError):
            code, status, message = "memory_invalid", 400, "Invalid Memory operation."
        except MemoryReadOnly:
            code, status, message = "memory_read_only", 409, "Shared Memory writing is unavailable."
        except Exception:
            code, status, message = "memory_unavailable", 503, "Memory operation could not be confirmed. Refresh before retrying."
        return self._disable_cache(web.json_response({"error": message, "code": code}, status=status))

    async def handle_voice_diagnostics(self, request):
        self._require_access(request)
        if request.query_string:
            raise web.HTTPBadRequest()
        from core.cluster.voice_diagnostics import PROTOCOL, safe_snapshot
        config = self.reconciliation.config
        peers = config.secrets
        nodes = peers.nodes if peers is not None else ((self.reconciliation.source['node_id'], None),)
        # Deployment-owned hosts only; no user URL, redirects, credentials or proxy.
        async with ClientSession(trust_env=False, timeout=ClientTimeout(total=2)) as session:
            async def one(node, endpoint):
                try:
                    host = urlsplit(endpoint).hostname if endpoint is not None else config.host
                    if host in {'0.0.0.0', '::'}:
                        host = '127.0.0.1' if host == '0.0.0.0' else '::1'
                    if ':' in host:
                        host = f'[{host}]'
                    url = f'http://{host}:{config.diagnostic_core_port}/diagnostics'
                    async with session.get(url, allow_redirects=False) as response:
                        if response.status != 200:
                            raise ValueError
                        data = bytearray()
                        async for chunk in response.content.iter_chunked(4096):
                            data.extend(chunk)
                            if len(data) > 32768:
                                raise ValueError
                    return {'state': 'ready', **safe_snapshot(json.loads(data), node)}
                except Exception:
                    return {'node_id': node, 'state': 'unavailable'}
            results = await asyncio.gather(*(one(node, endpoint) for node, endpoint in nodes))
        return self._disable_cache(web.json_response({'protocol': PROTOCOL,
            'scope': 'configured_peers' if peers is not None else 'local_only', 'nodes': results}))

    def _require_secret_access(self, request):
        self._require_access(request)
        if self.secrets is None:
            raise web.HTTPServiceUnavailable(text="Cluster secret provisioning is not configured")
        if request.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
            raise web.HTTPForbidden(text="Same-origin Settings access required")
        origin = request.headers.get("Origin")
        if origin is not None:
            parsed = urlsplit(origin)
            if parsed.scheme != request.scheme or parsed.netloc != request.host or parsed.path:
                raise web.HTTPForbidden(text="Same-origin Settings access required")

    async def handle_secret_status(self, request):
        self._require_secret_access(request)
        if set(request.query) != {"group", "provider", "field"}:
            raise web.HTTPBadRequest(text="Expected a provider credential target")
        try:
            result = await self.secrets.status(request.query["group"], request.query["provider"], request.query["field"])
        except (OSError, ValueError, TypeError, KeyError) as error:
            return self._error(error)
        return self._disable_cache(web.json_response(result))

    async def handle_secret_put(self, request):
        self._require_secret_access(request)
        self._require_json(request)
        if request.headers.get("X-Xiaozhi-Settings") != "1" or request.query_string:
            raise web.HTTPForbidden(text="Explicit Settings request required")
        from core.cluster.secret_provisioning import ProvisionIncomplete
        from core.cluster.secret_transport import decode
        try:
            data = await asyncio.wait_for(request.read(), timeout=2)
            if len(data) > 8192:
                raise ValueError
            body = decode(data)
            if not isinstance(body, dict) or set(body) != {"group", "provider", "field", "value", "base_revision"}:
                raise ValueError
            result = await self.secrets.provision(body["group"], body["provider"], body["field"], body["value"], body["base_revision"])
        except ProvisionIncomplete as error:
            return self._disable_cache(web.json_response({"committed": False, "code": "secret_provision_incomplete",
                "error": "Not every node confirmed the key. Shared configuration was not changed; retry when all nodes are available.",
                "nodes": error.nodes}, status=503))
        except ConfigConflict as error:
            return self._error(error)
        except (ConfigUnavailable, OSError):
            return self._disable_cache(web.json_response({"committed": None, "code": "secret_commit_unconfirmed",
                "error": "Secret save could not be confirmed. Sync Settings before retrying."}, status=503))
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, asyncio.TimeoutError):
            return self._disable_cache(web.json_response({"committed": False, "code": "invalid_secret_request",
                "error": "Select a supported provider API key and supply a valid value and current revision for all deployed members."}, status=400))
        # Cloud CAS already succeeded. A failed response refresh cannot turn the
        # committed key change into an apparent failed Save.
        try:
            payload = await self.reconciliation.operation(self.editor.read_public)
            result["settings"] = self._decorate(payload)
        except Exception:
            result["reload_required"] = True
        return self._disable_cache(web.json_response(result))

    async def handle_secret_peer(self, request):
        # This route never uses LAN/browser authorization or forwarded IPs.
        # Both the request and acknowledgement are authenticated ciphertext.
        if self.secrets is None or request.query_string or request.content_type != "application/json":
            raise web.HTTPNotFound()
        from core.cluster.secret_transport import decode
        try:
            data = await asyncio.wait_for(request.read(), timeout=2)
            envelope = decode(data)
            action = envelope.get("action")
            if action not in ("store", "status"):
                raise ValueError
            source, operation, payload = self.secrets.cipher.open(data, action, self.secrets.node, remote=request.remote)
            result = await self.reconciliation.operation(self.secrets.local, action, payload)
            response = self.secrets.cipher.seal("stored" if action == "store" else "checked",
                self.secrets.node, source, operation, result)
        except Exception:
            raise web.HTTPForbidden(text="Secret peer operation not confirmed") from None
        return self._disable_cache(web.json_response(response))

    async def handle_health(self, request):
        self._require_access(request)
        healthy = self.reconciliation.http_operational and self.reconciliation.healthy
        return self._disable_cache(web.json_response({"healthy": healthy}, status=200 if healthy else 503))

    async def handle_runtime_status(self, request):
        self._require_access(request)
        if self.runtime is None:
            raise web.HTTPNotFound()
        if request.query_string:
            raise web.HTTPBadRequest()
        return self._disable_cache(web.json_response(await self.runtime.status()))

    async def handle_runtime_apply(self, request):
        self._require_secret_access(request)
        self._require_json(request)
        if self.runtime is None:
            raise web.HTTPNotFound()
        if request.headers.get('X-Xiaozhi-Settings') != '1' or request.query_string:
            raise web.HTTPForbidden()
        from core.cluster.runtime_protocol import decode
        try:
            body = decode(await asyncio.wait_for(request.read(), 2))
            if not isinstance(body, dict) or set(body) != {'revision', 'recover'}:
                raise ValueError
            job = await self.runtime.start(body['revision'], body['recover'])
        except ConfigConflict:
            return self._disable_cache(web.json_response({'code': 'runtime_config_changed',
                'error': 'Cloud changed. Sync Settings before applying runtime.'}, status=409))
        except ConfigUnavailable:
            return self._disable_cache(web.json_response({'code': 'runtime_unavailable',
                'error': 'All three runtime agents must be available. Check runtime status for an interrupted operation.'}, status=503))
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, asyncio.TimeoutError):
            raise web.HTTPBadRequest(text='Expected the saved revision and recovery flag') from None
        return self._disable_cache(web.json_response({'protocol': 'xiaozhi-runtime-apply-v1', 'job': job}, status=202))

    async def handle_runtime_peer(self, request):
        if self.runtime is None or request.query_string or request.content_type != 'application/json':
            raise web.HTTPNotFound()
        from core.cluster.secret_transport import decode
        try:
            data = await asyncio.wait_for(request.read(), 2)
            decode(data)
            source, operation, payload = self.runtime.cipher.open(data, 'runtime-request',
                self.runtime.node, remote=request.remote)
            result = await self.runtime.receive(source, operation, payload)
            envelope = self.runtime.cipher.seal('runtime-response', self.runtime.node, source, operation, result)
        except Exception:
            raise web.HTTPForbidden(text='Runtime peer operation not confirmed') from None
        return self._disable_cache(web.json_response(envelope))
