"""Explicit process-local HTTP and reconciliation settings, separate from server.ip."""

import ipaddress
import math
import os
from dataclasses import dataclass

from core.cluster.nats_config import NatsConnectionConfig
from core.cluster.secret_transport import SecretProvisionConfig
from core.cluster.mqtt_bootstrap import MqttBootstrapConfig


@dataclass(frozen=True, slots=True)
class ControlPlaneConfig:
    nats: NatsConnectionConfig
    host: str = "127.0.0.1"
    port: int = 8004
    allow_remote: bool = False
    reconcile_interval: float = 45
    secrets: SecretProvisionConfig | None = None
    bootstrap: MqttBootstrapConfig | None = None

    @classmethod
    def from_env(cls):
        nats = NatsConnectionConfig.from_env()
        try:
            host = os.environ.get("XIAOZHI_CONTROL_PLANE_HOST", "127.0.0.1")
            ipaddress.ip_address(host)
            port = int(os.environ.get("XIAOZHI_CONTROL_PLANE_PORT", "8004"))
            interval = float(os.environ.get("XIAOZHI_CONFIG_RECONCILE_SECONDS", "45"))
            remote = os.environ.get("XIAOZHI_CONTROL_PLANE_ALLOW_REMOTE", "false").lower()
            if (not 1 <= port <= 65535 or not math.isfinite(interval)
                    or not 1 <= interval <= 3600 or remote not in {"true", "false"}):
                raise ValueError
        except ValueError:
            raise ValueError("Invalid control-plane host, port, access policy or reconciliation interval") from None
        return cls(nats, host, port, remote == "true", interval, SecretProvisionConfig.from_env(port),
                   MqttBootstrapConfig.from_env())
