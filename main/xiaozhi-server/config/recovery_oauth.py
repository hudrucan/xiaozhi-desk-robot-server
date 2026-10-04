"""Explicit installed-app OAuth; credentials stay in memory until publication."""

from pathlib import Path
from contextlib import contextmanager
import errno
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import logging
import os
import secrets
import shlex
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit
import webbrowser

from config.cloud_recovery import RecoveryError, parse_json
from config.config_store import canonical_bytes
from config.drive_transport import GoogleDriveTransport


class OAuthClientError(RecoveryError):
    message = "OAuth client unavailable or invalid. Import a downloaded Desktop app JSON using scripts/setup_google_drive.py --import-client FILE."


class OAuthDependencyError(RecoveryError):
    message = "Google OAuth dependencies unavailable; install the server requirements with the Python environment running this command."


class OAuthPortError(RecoveryError):
    message = "OAuth callback port is occupied; choose another --oauth-port and use that port in the SSH tunnel."


class OAuthLoginError(RecoveryError):
    message = "Google authorization did not complete. Check the SSH tunnel, consent and network, then retry with a new login link."


class OAuthStorageError(RecoveryError):
    message = "OAuth setup could not save local credentials, or existing application/account credentials differ. Existing files were not replaced."


class OAuthBusyError(RecoveryError):
    message = "Google authorization is already pending; finish that login or wait for it to expire."


_logging_guard = threading.Lock()
_logging_users = 0
_logging_previous = 0


@contextmanager
def _private_logging():
    # start and the worker overlap. Restore the original level only when both
    # have finished, rather than leaving global logging disabled after login.
    global _logging_users, _logging_previous
    with _logging_guard:
        if _logging_users == 0:
            _logging_previous = logging.root.manager.disable
            logging.disable(logging.CRITICAL)
        _logging_users += 1
    try:
        yield
    finally:
        with _logging_guard:
            _logging_users -= 1
            if _logging_users == 0:
                logging.disable(_logging_previous)


def load_oauth_client(path):
    """Validate Google's downloaded Desktop client without echoing its contents."""
    try:
        path = Path(path)
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError()
        config = parse_json(path.read_bytes())
        if not isinstance(config, dict) or set(config) != {"installed"}:
            raise ValueError()
        installed = config["installed"]
        if not isinstance(installed, dict) or any(
            not isinstance(installed.get(key), str) or not installed[key].strip()
            for key in ("client_id", "client_secret")
        ):
            raise ValueError()
        # OAuthlib accepts custom endpoints; a downloaded Google client must not
        # redirect token exchanges (including the client secret) elsewhere.
        if installed.get("auth_uri") not in {
            "https://accounts.google.com/o/oauth2/auth", "https://accounts.google.com/o/oauth2/v2/auth"
        } or installed.get("token_uri") != "https://oauth2.googleapis.com/token":
            raise ValueError()
        return config
    except Exception:
        raise OAuthClientError() from None


def install_oauth_file(path, content):
    """Durable, private, create-only publication; never replace another identity."""
    temporary = None
    try:
        path = Path(path)
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.exists():
            if canonical_bytes(parse_json(path.read_bytes())) != content:
                raise ValueError()
            os.chmod(path, 0o600)
            return
        fd, temporary = tempfile.mkstemp(prefix=".oauth-", dir=path.parent)
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        # link is atomic and fails if another process published the target first.
        os.link(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        raise OAuthStorageError() from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def browser_available():
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return False
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    try:
        webbrowser.get()
        return True
    except webbrowser.Error:
        return False


def add_oauth_arguments(parser):
    parser.add_argument("--no-browser", action="store_true", help="Print the Google login link without opening a browser")
    parser.add_argument("--oauth-port", type=int, default=8765, help="Loopback callback port (default: 8765)")
    parser.add_argument("--oauth-timeout", type=int, default=600, help="Login timeout in seconds (default: 600)")
    parser.add_argument("--ssh-target", help="SSH user@host shown in the laptop tunnel command")


def credential_payload(content):
    value = parse_json(content)
    if not isinstance(value, dict) or value.get("type") not in {"authorized_user", "service_account"}:
        raise RecoveryError()
    required = (("client_id", "client_secret", "refresh_token") if value["type"] == "authorized_user"
                else ("client_email", "private_key", "token_uri"))
    if any(not isinstance(value.get(key), str) or not value[key] for key in required):
        raise RecoveryError()
    return value


class _QuietLoopbackServer(HTTPServer):
    allow_reuse_address = False

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(2)
        return connection, address

    def handle_error(self, request, client_address):
        pass  # Never traceback a request carrying an OAuth callback code.


class OAuthLoopbackSession:
    """One process-owned PKCE session, with a quiet IPv4 callback worker.

    start() returns the login URL immediately; wait() consumes credentials after
    the worker validates state and exchanges the code. close() cancels the wait.
    Neither this primitive nor the CLI persists credentials. The explicit setup
    caller uses install_oauth_file; restore retains its journal publication.
    """
    _guard = threading.Lock()
    _owner = None

    def __init__(self, client_path, *, port=8765, timeout=600, flow_factory=None, server_factory=_QuietLoopbackServer):
        self.client_path = client_path
        self.port, self.timeout = port, timeout
        self.flow_factory, self.server_factory = flow_factory, server_factory
        self._stop, self._done = threading.Event(), threading.Event()
        self._lock = threading.Lock()
        self._status, self._error, self._payload = "idle", None, None
        self._flow = self._state = self._callback = self._server = self._thread = None

    def status(self):
        with self._lock:
            return {"status": self._status, **({"error": self._error.message} if self._error else {})}

    def start(self):
        with _private_logging():
            return self._start()

    def _start(self):
        with self._guard:
            if OAuthLoopbackSession._owner is not None:
                raise OAuthBusyError()
            if self._status != "idle":
                raise OAuthLoginError()
            OAuthLoopbackSession._owner = self
        try:
            if type(self.port) is not int or not 1 <= self.port <= 65535 or type(self.timeout) is not int or not 1 <= self.timeout <= 3600:
                raise OAuthLoginError()
            config = load_oauth_client(self.client_path)
            factory = self.flow_factory
            if factory is None:
                try:
                    from google_auth_oauthlib.flow import InstalledAppFlow
                except ImportError:
                    raise OAuthDependencyError() from None
                factory = InstalledAppFlow.from_client_config
            self._state = secrets.token_urlsafe(32)
            self._flow = factory(config, scopes=GoogleDriveTransport.SCOPES,
                                 state=self._state, autogenerate_code_verifier=True)
            owner = self

            class CallbackHandler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass  # Callback paths contain the authorization code.

                def do_GET(self):
                    valid = owner._accept_callback(self.path)
                    self.send_response(200 if valid else 400)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(b"Google callback received. Return to setup." if valid else b"Google authorization rejected. Retry from setup.")

            self._server = self.server_factory(("127.0.0.1", self.port), CallbackHandler)
            self._server.timeout = 0.25
            self._flow.redirect_uri = f"http://127.0.0.1:{self.port}/"
            url, state = self._flow.authorization_url(access_type="offline", prompt="consent", state=self._state)
            if state != self._state:
                raise OAuthLoginError()
            self._deadline = time.monotonic() + self.timeout
            self._status = "pending"
            self._thread = threading.Thread(target=self._run, name="google-oauth-loopback", daemon=True)
            self._thread.start()
            return url
        except Exception as error:
            safe = self._safe_error(error)
            self._fail(safe)
            self._release()
            raise safe from None

    @staticmethod
    def _safe_error(error):
        if isinstance(error, (OAuthClientError, OAuthDependencyError, OAuthLoginError, OAuthBusyError)):
            return error
        if isinstance(error, OSError) and error.errno == errno.EADDRINUSE:
            return OAuthPortError()
        return OAuthLoginError()

    def _fail(self, error):
        with self._lock:
            self._status, self._error, self._payload = "failed", error, None

    def _accept_callback(self, path):
        # Ignore probes such as favicon without consuming the OAuth session.
        try:
            if urlsplit(path).path != "/":
                return False
            query = parse_qs(urlsplit(path).query, strict_parsing=True)
            if len(path) > 8192 or any(len(value) != 1 for value in query.values()):
                raise ValueError()
            state = query.get("state", [""])[0]
            if not hmac.compare_digest(state, self._state) or not query.get("code") or "error" in query:
                raise ValueError()
            self._callback = f"https://127.0.0.1:{self.port}{path}"
            return True
        except Exception:
            self._fail(OAuthLoginError())
            self._stop.set()
            return False

    def _run(self):
        with _private_logging():
            self._exchange()

    def _exchange(self):
        try:
            while not self._stop.is_set() and self._callback is None:
                if time.monotonic() >= self._deadline:
                    raise OAuthLoginError()
                self._server.handle_request()
            if self._stop.is_set():
                raise OAuthLoginError()
            self._flow.fetch_token(authorization_response=self._callback, timeout=20)
            payload = credential_payload(canonical_bytes({"type": "authorized_user",
                **parse_json(self._flow.credentials.to_json().encode("utf-8"))}))
            if payload["type"] != "authorized_user" or self._stop.is_set():
                raise OAuthLoginError()
            with self._lock:
                self._payload, self._status = canonical_bytes(payload), "authorized"
        except Exception as error:
            self._fail(self._safe_error(error))
        finally:
            self._release()

    def _release(self):
        try:
            if self._server is not None:
                self._server.server_close()
        except Exception:
            pass  # Cleanup diagnostics must not traceback OAuth state.
        finally:
            self._flow = self._state = self._callback = None
            with self._guard:
                if OAuthLoopbackSession._owner is self:
                    OAuthLoopbackSession._owner = None
            self._done.set()

    def wait(self):
        self._done.wait()
        with self._lock:
            if self._status != "authorized":
                raise self._error or OAuthLoginError()
            return self._payload

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=21)
        with self._lock:
            self._payload = None


def authorized_session(content):
    """Create the same narrow transport session for UI and CLI callers."""
    try:
        from google.auth.transport.requests import AuthorizedSession
        payload = credential_payload(content)
        if payload["type"] == "authorized_user":
            from google.oauth2.credentials import Credentials
            credentials = Credentials.from_authorized_user_info(payload, scopes=GoogleDriveTransport.SCOPES)
        else:
            from google.oauth2 import service_account
            credentials = service_account.Credentials.from_service_account_info(payload, scopes=GoogleDriveTransport.SCOPES)
        return AuthorizedSession(credentials, refresh_timeout=20)
    except ImportError:
        raise OAuthDependencyError() from None
    except Exception:
        raise OAuthLoginError() from None


def obtain_credentials(*, existing_path=None, client_path=None, flow_factory=None, open_browser=None,
                       oauth_port=8765, oauth_timeout=600, ssh_target=None):
    """CLI adapter over the same session as setup UI; no early persistence."""
    session = None
    old_logging_level = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        if existing_path is not None:
            content = canonical_bytes(credential_payload(Path(existing_path).read_bytes()))
        else:
            target = ssh_target or "user@server"
            if not isinstance(target, str) or target.startswith("-") or any(ord(c) < 32 or ord(c) == 127 for c in target):
                raise OAuthLoginError()
            if open_browser is None:
                open_browser = browser_available()
            print(f"Google login: waiting up to {oauth_timeout} seconds; callback port {oauth_port}.")
            if not open_browser:
                print("If opening the link on another machine, run this there in a separate terminal first:")
                print(f"ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:{oauth_port}:127.0.0.1:{oauth_port} {shlex.quote(target)}")
                if ssh_target is None:
                    print("Replace user@server with the SSH destination you normally use.")
                print("Keep the tunnel open, then open the Google login link below on that machine.")
            session = OAuthLoopbackSession(client_path, port=oauth_port, timeout=oauth_timeout, flow_factory=flow_factory)
            url = session.start()
            print(f"Authorize Google using this URL: {url}")
            if open_browser:
                try:
                    webbrowser.open(url, new=1, autoraise=True)
                except webbrowser.Error:
                    pass  # Link is already visible for manual browser use.
            content = session.wait()
        return content, authorized_session(content)
    except (OAuthClientError, OAuthDependencyError, OAuthLoginError, OAuthPortError, OAuthBusyError):
        raise
    except OSError as error:
        if error.errno == errno.EADDRINUSE:
            raise OAuthPortError() from None
        raise OAuthLoginError() from None
    except Exception:
        raise OAuthLoginError() from None
    finally:
        if session is not None:
            session.close()
        logging.disable(old_logging_level)
