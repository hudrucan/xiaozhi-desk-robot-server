"""Local first-run administration, delegating all recovery to existing stores."""

import asyncio
from email.parser import BytesParser
from email.policy import default as email_policy
import ipaddress
from pathlib import Path
import tempfile
from urllib.parse import unquote, urlsplit

from aiohttp import web

from config.cloud_recovery import RecoveryConflict, RecoveryError, RecoveryIdentityConflict, discover_sources
from config.config_loader import get_project_dir
from config.config_store import canonical_bytes
from config.drive_transport import GoogleDriveTransport
from config.first_run import FirstRunError, finish_first_run
from config.node_identity import hostname_node_id
from config.recovery_oauth import (
    OAuthBusyError, OAuthLoopbackSession, OAuthStorageError, authorized_session,
    credential_payload, install_oauth_file, load_oauth_client,
)

MAX_CLIENT_BYTES = 32 * 1024


def _response(value, status=200):
    return web.json_response(value, status=status)


@web.middleware
async def setup_access(request, handler):
    """Loopback plus browser origin/Host checks; setup has no CORS or LAN mode."""
    peer = request.transport.get_extra_info("peername") if request.transport else None
    try:
        if not peer or not ipaddress.ip_address(peer[0]).is_loopback:
            raise ValueError()
        hostname = urlsplit("http://" + request.host).hostname
        if hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError()
        if request.headers.get("Origin") not in {None, f"http://{request.host}"}:
            raise ValueError()
        if request.method == "POST" and request.headers.get("X-Setup-Request") != "1":
            raise ValueError()
    except (ValueError, TypeError):
        return _response({"error": "Setup accepts same-origin localhost requests only."}, 403)
    try:
        response = await handler(request)
    except RecoveryConflict as error:
        response = _response({"error": error.message}, 409)
    except OAuthBusyError as error:
        response = _response({"error": error.message}, 409)
    except RecoveryError as error:
        response = _response({"error": error.message}, 503)
    except FirstRunError:
        response = _response({"error": "Setup completion could not be saved; retry restore before restarting."}, 503)
    except web.HTTPException as error:
        response = _response({"error": error.reason}, error.status)
    except Exception:
        response = _response({"error": "Cloud setup unavailable; check authorization and retry."}, 503)
    response.headers.update({
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
        "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    })
    return response


class SetupHandler:
    def __init__(self, request_restart, *, data_dir=None, session_factory=OAuthLoopbackSession,
                 transport_factory=GoogleDriveTransport, discover=discover_sources, restorer=None, cloner=None):
        self.data = Path(data_dir or Path(get_project_dir()) / "data")
        self.web_dir = Path(get_project_dir()) / "web/setup"
        self.request_restart = request_restart
        self.session_factory, self.transport_factory = session_factory, transport_factory
        self.discover, self.restorer = discover, restorer
        self.cloner = cloner
        self._mutation = asyncio.Lock()
        self._jobs = set()
        self._oauth = self._oauth_task = None
        self._oauth_status = {"status": "idle"}
        self._completed = False

    def _pending(self):
        if self._completed or not (self.data / ".first-run").is_file():
            raise web.HTTPConflict(reason="Setup already completed; restart the server.")
        if self.data.is_symlink() or (self.data / ".first-run").is_symlink():
            raise RecoveryError()

    async def _job(self, function, *args, **kwargs):
        # Client disconnect must not orphan a publishing worker or release the
        # transaction lock while it is still changing local recovery files.
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        self._jobs.add(task)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
        finally:
            self._jobs.discard(task)

    def _credentials(self):
        path = self.data / "drive-credentials.json"
        if self.data.is_symlink() or path.is_symlink():
            raise OAuthStorageError()
        content = path.read_bytes()
        payload = credential_payload(content)
        client = load_oauth_client(self.data / "oauth-client.json")
        if payload["type"] != "authorized_user" or payload["client_id"] != client["installed"]["client_id"]:
            raise OAuthStorageError()
        return canonical_bytes(payload)

    def _transport(self):
        content = self._credentials()
        return content, self.transport_factory(self.data / "drive-credentials.json", session=authorized_session(content))

    async def handle_redirect(self, request):
        return web.Response(status=302, headers={"Location": "/setup/"})

    async def handle_index(self, request):
        return web.FileResponse(self.web_dir / "index.html")

    async def handle_asset(self, request):
        name = request.match_info["filename"]
        if name not in {"app.js", "styles.css"}:
            raise web.HTTPNotFound()
        return web.FileResponse(self.web_dir / name)

    async def handle_status(self, request):
        from config.cloud_clone import clone_selection
        return _response({"setup_required": not self._completed, "completed": self._completed,
            "client_installed": (self.data / "oauth-client.json").is_file(),
            "credentials_installed": (self.data / "drive-credentials.json").is_file(),
            "hostname": hostname_node_id(), "clone_selection": await self._job(clone_selection, self.data)})

    def _install_client(self, content):
        temporary = None
        try:
            if self.data.is_symlink():
                raise RecoveryError()
            with tempfile.NamedTemporaryFile(dir=self.data, prefix=".setup-client-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
            config = load_oauth_client(temporary)
            credential_path = self.data / "drive-credentials.json"
            if credential_path.exists():
                if credential_path.is_symlink():
                    raise OAuthStorageError()
                payload = credential_payload(credential_path.read_bytes())
                if payload["type"] != "authorized_user" or payload["client_id"] != config["installed"]["client_id"]:
                    raise OAuthStorageError()
            install_oauth_file(self.data / "oauth-client.json", canonical_bytes(config))
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    async def handle_client(self, request):
        async with self._mutation:
            self._pending()
            if self._oauth_task is not None and not self._oauth_task.done():
                raise OAuthBusyError()
            if request.content_type != "multipart/form-data":
                raise web.HTTPUnsupportedMediaType()
            raw = await request.read()  # Application bounds the entire multipart envelope.
            message = BytesParser(policy=email_policy).parsebytes(
                b"Content-Type: " + request.headers["Content-Type"].encode("ascii") + b"\r\nMIME-Version: 1.0\r\n\r\n" + raw)
            parts = list(message.iter_parts())
            if len(parts) != 1 or parts[0].get_param("name", header="content-disposition") != "file":
                raise web.HTTPBadRequest()
            filename = unquote(parts[0].get_filename() or "")
            if not filename or filename in {".", ".."} or "/" in filename or "\\" in filename or any(ord(c) < 32 for c in filename):
                raise web.HTTPBadRequest()
            content = parts[0].get_payload(decode=True)
            if not isinstance(content, bytes) or len(content) > MAX_CLIENT_BYTES:
                raise web.HTTPRequestEntityTooLarge(max_size=MAX_CLIENT_BYTES, actual_size=len(content or b""))
            await self._job(self._install_client, content)
            return _response({"client_installed": True})

    async def _finish_oauth(self, session):
        try:
            content = await self._job(session.wait)
            # Validate the returned identity before create-only persistence.
            payload = credential_payload(content)
            client = load_oauth_client(self.data / "oauth-client.json")
            if payload["type"] != "authorized_user" or payload["client_id"] != client["installed"]["client_id"]:
                raise OAuthStorageError()
            await self._job(install_oauth_file, self.data / "drive-credentials.json", canonical_bytes(payload))
            self._oauth_status = {"status": "authorized"}
        except RecoveryError as error:
            self._oauth_status = {"status": "failed", "error": error.message}
        except Exception:
            self._oauth_status = {"status": "failed", "error": "Google authorization could not be saved; retry setup."}
        finally:
            await asyncio.to_thread(session.close)

    async def handle_oauth_start(self, request):
        async with self._mutation:
            self._pending()
            if self._oauth_task is not None and not self._oauth_task.done():
                raise OAuthBusyError()
            if (self.data / "drive-credentials.json").exists():
                await self._job(self._credentials)
                self._oauth_status = {"status": "authorized"}
                return _response(self._oauth_status)
            session = self.session_factory(self.data / "oauth-client.json", port=8765, timeout=600)
            self._oauth = session
            try:
                url = await self._job(session.start)
            except BaseException:
                await asyncio.to_thread(session.close)
                self._oauth = None
                raise
            self._oauth_status = {"status": "pending"}
            self._oauth_task = asyncio.create_task(self._finish_oauth(session))
            return _response({"status": "pending", "authorization_url": url})

    async def handle_oauth_status(self, request):
        return _response(self._oauth_status)

    def _sources(self):
        _, transport = self._transport()
        return [source.metadata() for source in self.discover(transport)]

    async def handle_sources(self, request):
        self._pending()
        sources = await self._job(self._sources)
        hostname = hostname_node_id()
        source = sources[0] if len(sources) == 1 else None
        nodes = source["nodes"] if source else []
        node = hostname if hostname in nodes else nodes[0] if len(nodes) == 1 else None
        return _response({"sources": sources, "hostname": hostname,
            "selected_source_id": source["source_id"] if source else None, "selected_node_id": node})

    def _restore(self, body):
        # Recovery readiness needs asset validators, but opening the setup UI
        # must not import audio/runtime dependencies before this explicit step.
        from config.cloud_restore import restore_cloud_node, select_node, select_source
        from config.cloud_clone import clone_cloud_node, clone_selection

        content, transport = self._transport()
        # Browser selection is never a descriptor authority. Rediscover at submit.
        source = select_source(self.discover(transport), body["source_id"])
        node = select_node(source, body["node_id"])
        if body.get("mode") == "clone":
            result = (self.cloner or clone_cloud_node)(source, node, body["new_node_id"], body["passphrase"],
                content, transport, data_dir=self.data)
        else:
            if clone_selection(self.data) is not None:
                raise RecoveryIdentityConflict()
            result = (self.restorer or restore_cloud_node)(source, node, body["passphrase"], content, transport, data_dir=self.data, activate=True)
        if result.provider != "google_drive":
            raise RecoveryError()
        finish_first_run(self.data)
        # Also record completion when the browser disconnects after publication.
        # This is a single boolean; no async resource is mutated by the worker.
        self._completed = True
        return {"config": "OK", "soundbank": "OK", "memory": "OK", "secrets": "OK", "provider": "google_drive"}

    async def handle_restore(self, request):
        async with self._mutation:
            self._pending()
            if self._oauth_task is not None and not self._oauth_task.done():
                raise OAuthBusyError()
            if request.content_type != "application/json":
                raise web.HTTPUnsupportedMediaType()
            body = await request.json()
            keys = {"source_id", "node_id", "passphrase"}
            clone = isinstance(body, dict) and body.get("mode") == "clone"
            if clone:
                keys |= {"mode", "new_node_id"}
            if not isinstance(body, dict) or set(body) != keys or any(
                not isinstance(body[key], str) or not body[key] or len(body[key]) > limit
                for key, limit in (("source_id", 36), ("node_id", 192), ("passphrase", 4096)) +
                    ((("new_node_id", 192),) if clone else ())
            ):
                raise web.HTTPBadRequest()
            if clone:
                from config.cloud_clone import validate_new_node_id
                try:
                    validate_new_node_id(body["new_node_id"])
                except RecoveryError:
                    raise web.HTTPBadRequest(reason="Enter a valid new node ID using letters, digits, dots, underscores or hyphens.") from None
            try:
                summary = await self._job(self._restore, body)
            finally:
                body.clear()  # Do not retain recovery passphrase in handler state.
            self._completed = True
            return _response(summary)

    async def handle_restart(self, request):
        if not self._completed or (self.data / ".first-run").exists():
            raise web.HTTPConflict(reason="Complete restore before restarting.")
        # Give the success response time to reach the browser, as Settings does.
        asyncio.get_running_loop().call_later(0.5, self.request_restart)
        return _response({"restarting": True})

    async def close(self):
        if self._oauth is not None:
            await asyncio.to_thread(self._oauth.close)
        if self._oauth_task is not None:
            await asyncio.gather(self._oauth_task, return_exceptions=True)
        if self._jobs:
            await asyncio.gather(*tuple(self._jobs), return_exceptions=True)
