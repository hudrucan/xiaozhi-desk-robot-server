"""Bounded, non-secret Core NATS hints; the Cloud manifest is the authority."""

import json

CONFIG_CHANGED_SUBJECT = "xiaozhi.v1.config.changed"
CONFIG_PROTOCOL = "xiaozhi-config-v1"
MAX_EVENT_BYTES = 256
MAX_REVISION = (1 << 63) - 1


def config_changed(revision):
    if type(revision) is not int or not 1 <= revision <= MAX_REVISION:
        raise ValueError("Invalid config revision hint")
    return json.dumps({"protocol": CONFIG_PROTOCOL, "revision": revision},
                      separators=(",", ":")).encode("utf-8")


def _unique_object(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("Duplicate hint fields")
    return value


def parse_config_changed(data):
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_EVENT_BYTES:
        return None
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)
        if (not isinstance(value, dict) or set(value) != {"protocol", "revision"}
                or value["protocol"] != CONFIG_PROTOCOL):
            return None
        config_changed(value["revision"])
        return value["revision"]
    except (ValueError, TypeError, RecursionError):
        return None
