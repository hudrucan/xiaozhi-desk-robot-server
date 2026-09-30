"""Shared helpers for identifying secret configuration fields."""

import re


SECRET_NAMES = {
    "access_key",
    "access_key_secret",
    "access_token",
    "api_key",
    "auth_key",
    "authorization",
    "client_secret",
    "mcp_endpoint",
    "mqtt_signature_key",
    "password",
    "personal_access_token",
    "private_key",
    "secret",
    "secret_key",
    "token",
}
COMPACT_SECRET_NAMES = {
    "accesskey",
    "accesstoken",
    "apikey",
    "authkey",
    "authtoken",
    "clientsecret",
    "privatekey",
    "secretkey",
}


def normalize_config_name(key):
    """Normalize snake, kebab, dotted, and camel-case configuration keys."""
    normalized = re.sub(
        r"([A-Z]+)([A-Z][a-z])", r"\1_\2", str(key).strip()
    )
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", normalized)
    return normalized.lower().replace("-", "_").replace(".", "_")


def is_secret_name(key):
    """Return whether a configuration key conventionally contains a secret."""
    normalized = normalize_config_name(key)
    compact = normalized.replace("_", "")
    return (
        normalized in SECRET_NAMES
        or compact in COMPACT_SECRET_NAMES
        or normalized.endswith(("_token", "_secret"))
    )


def is_credential_name(key):
    """Apply the broader credential filter used for persisted provenance."""
    normalized = normalize_config_name(key)
    return is_secret_name(normalized) or normalized in {
        "auth",
        "bearer",
        "cookie",
        "credential",
        "credentials",
        "jwt",
        "key",
        "sig",
        "signature",
    } or normalized.endswith(
        (
            "_access_key",
            "_api_key",
            "_auth",
            "_authorization",
            "_bearer",
            "_cookie",
            "_credential",
            "_credentials",
            "_jwt",
            "_key",
            "_password",
            "_private_key",
            "_sig",
            "_signature",
        )
    ) or normalized.startswith(
        (
            "access_token_",
            "api_key_",
            "auth_",
            "authorization_",
            "bearer_",
            "client_secret_",
            "cookie_",
            "credential_",
            "jwt_",
            "password_",
            "private_key_",
            "secret_",
            "token_",
        )
    )
