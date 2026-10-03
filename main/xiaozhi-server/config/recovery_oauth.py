"""Explicit installed-app OAuth; credentials stay in memory until publication."""

from pathlib import Path
import logging

from config.cloud_recovery import RecoveryError, parse_json
from config.config_store import canonical_bytes
from config.drive_transport import GoogleDriveTransport


def credential_payload(content):
    value = parse_json(content)
    if not isinstance(value, dict) or value.get("type") not in {"authorized_user", "service_account"}:
        raise RecoveryError()
    required = (("client_id", "client_secret", "refresh_token") if value["type"] == "authorized_user"
                else ("client_email", "private_key", "token_uri"))
    if any(not isinstance(value.get(key), str) or not value[key] for key in required):
        raise RecoveryError()
    return value


def obtain_credentials(*, existing_path=None, client_path=None, flow_factory=None, open_browser=True):
    """Return private credential bytes/session, never persist or print user tokens.

    Desktop OAuth client configuration is an external application identity, not
    a user token. The same client must be used as the app owning the Drive files.
    google-auth-oauthlib implements the supported loopback flow with PKCE.
    """
    old_logging_level = logging.root.manager.disable
    # OAuth libraries can debug-log token exchange bodies or callback codes.
    logging.disable(logging.CRITICAL)
    try:
        from google.auth.transport.requests import AuthorizedSession
        if existing_path is not None:
            payload = credential_payload(Path(existing_path).read_bytes())
            if payload["type"] == "authorized_user":
                from google.oauth2.credentials import Credentials
                credentials = Credentials.from_authorized_user_info(payload, scopes=GoogleDriveTransport.SCOPES)
            else:
                from google.oauth2 import service_account
                credentials = service_account.Credentials.from_service_account_info(payload, scopes=GoogleDriveTransport.SCOPES)
        else:
            if client_path is None:
                raise RecoveryError()
            config = parse_json(Path(client_path).read_bytes())
            if not isinstance(config, dict) or set(config) != {"installed"} or not isinstance(config["installed"], dict):
                raise RecoveryError()
            if flow_factory is None:
                from google_auth_oauthlib.flow import InstalledAppFlow
                flow_factory = InstalledAppFlow.from_client_config
            flow = flow_factory(config, scopes=GoogleDriveTransport.SCOPES, autogenerate_code_verifier=True)
            credentials = flow.run_local_server(host="localhost", port=0, open_browser=open_browser,
                timeout_seconds=180, access_type="offline", prompt="consent",
                authorization_prompt_message="Authorize Google using this URL: {url}",
                success_message="Google authorization completed. You may close this window.")
            payload = credential_payload(canonical_bytes({"type": "authorized_user", **parse_json(credentials.to_json().encode("utf-8"))}))
        return canonical_bytes(payload), AuthorizedSession(credentials, refresh_timeout=20)
    except Exception:
        raise RecoveryError() from None
    finally:
        logging.disable(old_logging_level)
