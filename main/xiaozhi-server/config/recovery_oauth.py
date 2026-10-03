"""Explicit installed-app OAuth; credentials stay in memory until publication."""

from pathlib import Path
import errno
import logging
import os
import shlex
import sys
import tempfile
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


def load_oauth_client(path):
    """Validate Google's downloaded Desktop client without echoing its contents."""
    try:
        config = parse_json(Path(path).read_bytes())
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


def obtain_credentials(*, existing_path=None, client_path=None, flow_factory=None, open_browser=None,
                       oauth_port=8765, oauth_timeout=600, ssh_target=None):
    """Return private credential bytes/session, never persist or print user tokens.

    Desktop OAuth client configuration is an external application identity, not
    a user token. The same client must be used as the app owning the Drive files.
    google-auth-oauthlib implements the supported loopback flow with PKCE.
    """
    old_logging_level = logging.root.manager.disable
    # OAuth libraries can debug-log token exchange bodies or callback codes.
    logging.disable(logging.CRITICAL)
    try:
        try:
            from google.auth.transport.requests import AuthorizedSession
        except ImportError:
            raise OAuthDependencyError() from None
        if existing_path is not None:
            payload = credential_payload(Path(existing_path).read_bytes())
            if payload["type"] == "authorized_user":
                from google.oauth2.credentials import Credentials
                credentials = Credentials.from_authorized_user_info(payload, scopes=GoogleDriveTransport.SCOPES)
            else:
                from google.oauth2 import service_account
                credentials = service_account.Credentials.from_service_account_info(payload, scopes=GoogleDriveTransport.SCOPES)
        else:
            config = load_oauth_client(client_path)
            if flow_factory is None:
                try:
                    from google_auth_oauthlib.flow import InstalledAppFlow
                except ImportError:
                    raise OAuthDependencyError() from None
                flow_factory = InstalledAppFlow.from_client_config
            if type(oauth_port) is not int or not 1 <= oauth_port <= 65535 or type(oauth_timeout) is not int or not 1 <= oauth_timeout <= 3600:
                raise OAuthLoginError()
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
            flow = flow_factory(config, scopes=GoogleDriveTransport.SCOPES, autogenerate_code_verifier=True)
            credentials = flow.run_local_server(host="127.0.0.1", port=oauth_port, open_browser=open_browser,
                timeout_seconds=oauth_timeout, access_type="offline", prompt="consent",
                authorization_prompt_message="Authorize Google using this URL: {url}",
                success_message="Google authorization completed. You may close this window.")
            payload = credential_payload(canonical_bytes({"type": "authorized_user", **parse_json(credentials.to_json().encode("utf-8"))}))
        return canonical_bytes(payload), AuthorizedSession(credentials, refresh_timeout=20)
    except (OAuthClientError, OAuthDependencyError, OAuthLoginError):
        raise
    except OSError as error:
        if error.errno == errno.EADDRINUSE:
            raise OAuthPortError() from None
        raise OAuthLoginError() from None
    except Exception:
        raise OAuthLoginError() from None
    finally:
        logging.disable(old_logging_level)
