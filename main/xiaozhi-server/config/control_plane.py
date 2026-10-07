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
    diagnostic_core_port: int = 8000
    runtime_apply: bool = False

    @classmethod
    def from_env(cls):
        nats = NatsConnectionConfig.from_env()
        try:
            host = os.environ.get("XIAOZHI_CONTROL_PLANE_HOST", "127.0.0.1")
            ipaddress.ip_address(host)
            port = int(os.environ.get("XIAOZHI_CONTROL_PLANE_PORT", "8004"))
            interval = float(os.environ.get("XIAOZHI_CONFIG_RECONCILE_SECONDS", "45"))
            remote = os.environ.get("XIAOZHI_CONTROL_PLANE_ALLOW_REMOTE", "false").lower()
            core_port = int(os.environ.get('XIAOZHI_DIAGNOSTIC_CORE_PORT', '8000'))
            apply = os.environ.get('XIAOZHI_RUNTIME_APPLY_ENABLED', 'false')
            if (not 1 <= port <= 65535 or not math.isfinite(interval)
                    or not 1 <= interval <= 3600 or remote not in {"true", "false"}
                    or not 1024 <= core_port <= 65535 or apply not in {'true', 'false'}):
                raise ValueError
        except ValueError:
            raise ValueError("Invalid control-plane host, port, access policy or reconciliation interval") from None
        secrets = SecretProvisionConfig.from_env(port)
        if apply == 'true' and secrets is None:
            raise ValueError('Runtime apply requires authenticated three-node peer configuration')
        return cls(nats, host, port, remote == "true", interval, secrets,
                   MqttBootstrapConfig.from_env(), core_port, apply == 'true')
