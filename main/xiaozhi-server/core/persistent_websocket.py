"""Pure capability negotiation helpers for Persistent WebSocket v1."""


PERSISTENT_WEBSOCKET_FEATURE = "desk_robot_persistent_ws_v1"


def negotiate_persistent_websocket(client_features, from_mqtt_gateway=False) -> bool:
    """Enable v1 only when this is WS and the client explicitly opted in."""
    return bool(
        not from_mqtt_gateway
        and isinstance(client_features, dict)
        and client_features.get(PERSISTENT_WEBSOCKET_FEATURE)
    )
