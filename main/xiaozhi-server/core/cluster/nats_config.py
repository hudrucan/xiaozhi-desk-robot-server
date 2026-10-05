"""Environment-only configuration for the standalone worker."""

import ipaddress
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from config.node_identity import hostname_node_id


class WorkerConfigError(ValueError):
    """Invalid worker configuration, with a credential-safe error message."""


def validate_worker_id(worker_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,192}", worker_id):
        raise WorkerConfigError(
            "XIAOZHI_WORKER_ID must contain 1-192 ASCII letters, digits, '_' or '-'"
        )
    return worker_id


def _validate_server(server: str) -> str:
    error = (
        "XIAOZHI_NATS_SERVERS must contain nats://host[:port] URLs "
        "without credentials, paths, queries or fragments"
    )
    try:
        url = urlsplit(server)
        if (
            not server.startswith("nats://")
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.path
            or "?" in server
            or "#" in server
            or any(char.isspace() or ord(char) < 32 for char in server)
            or (url.port is not None and not 1 <= url.port <= 65535)
            or url.netloc.endswith(":")
        ):
            raise ValueError
        try:
            ipaddress.ip_address(url.hostname)
        except ValueError:
            if len(url.hostname) > 253 or any(
                not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                for label in url.hostname.removesuffix(".").split(".")
            ):
                raise ValueError
    except ValueError:
        # URL parser errors can contain the supplied URL; do not expose them.
        raise WorkerConfigError(error) from None
    return server


@dataclass(frozen=True, slots=True)
class NatsConfig:
    servers: tuple[str, ...] = field(repr=False)
    username: str = field(repr=False)
    password: str = field(repr=False)
    worker_id: str

    @classmethod
    def from_env(cls) -> "NatsConfig":
        entries = os.environ.get("XIAOZHI_NATS_SERVERS", "").split(",")
        if any(not entry.strip() for entry in entries):
            raise WorkerConfigError(
                "XIAOZHI_NATS_SERVERS requires a nonempty comma-separated URL list"
            )
        servers = tuple(_validate_server(entry.strip()) for entry in entries)
        username = os.environ.get("XIAOZHI_NATS_USER", "")
        password = os.environ.get("XIAOZHI_NATS_PASSWORD", "")
        if not username.strip() or not password.strip():
            raise WorkerConfigError(
                "XIAOZHI_NATS_USER and XIAOZHI_NATS_PASSWORD are required"
            )
        # Hostname identities may contain dots; subjects require a single token.
        worker_id = os.environ.get("XIAOZHI_WORKER_ID")
        if worker_id is None:
            worker_id = hostname_node_id().replace(".", "-")
        return cls(servers, username, password, validate_worker_id(worker_id))
