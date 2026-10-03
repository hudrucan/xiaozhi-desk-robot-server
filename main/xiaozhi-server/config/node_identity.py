"""Hostname defaults for new local identities; persisted node IDs stay exact."""

import re
import socket

FALLBACK_NODE_ID = "local-node"
_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def normalize_hostname(hostname):
    if not isinstance(hostname, str):
        return FALLBACK_NODE_ID
    hostname = hostname.strip().removesuffix(".")
    if not hostname or len(hostname) > 192 or any(
        not _HOST_LABEL.fullmatch(label) for label in hostname.split(".")
    ):
        return FALLBACK_NODE_ID
    # Preserve case because existing logical node IDs are case-sensitive.
    return hostname


def hostname_node_id():
    try:
        return normalize_hostname(socket.gethostname())
    except OSError:
        return FALLBACK_NODE_ID
