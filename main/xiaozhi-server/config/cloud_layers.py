"""Override-only V1 topology; legacy sources retain their original semantics."""

import copy

from config.config_loader import load_default_config, merge_configs
from config.cloud_secrets import validate_cloud_secrets

BOOTSTRAP_ROOTS = {"node_id", "config_provider", "google_drive", "credentials_path"}
LEGACY_LAYERS = {"defaults", "overrides"}
CENTRAL_LAYERS = {"global", "environments", "roles", "nodes"}


def centralized(obj):
    return set(obj["layers"]) == CENTRAL_LAYERS


def legacy_centralized(obj):
    return centralized(obj) and set(obj["layers"]["global"]) == LEGACY_LAYERS


def _config_layer(value):
    if not isinstance(value, dict):
        raise ValueError("Cloud configuration layers must be objects")
    if BOOTSTRAP_ROOTS & set(value):
        raise ValueError("Cloud configuration must not contain local bootstrap state")
    validate_cloud_secrets(value)


def _named_layers(value):
    if not isinstance(value, dict) or any(
        not isinstance(name, str) or not name.strip() for name in value
    ):
        raise ValueError("Cloud scope names must be non-empty strings")
    for layer in value.values():
        _config_layer(layer)


def validate_layers(obj, validator, repo_defaults=None):
    layers = obj.get("layers")
    if not isinstance(layers, dict):
        raise ValueError("Invalid cloud configuration layers")
    if set(layers) == LEGACY_LAYERS:
        for layer in layers.values():
            _config_layer(layer)
        validator(merge_configs(layers["defaults"], layers["overrides"]))
        return
    if set(layers) != CENTRAL_LAYERS:
        raise ValueError("Invalid cloud configuration scopes")
    global_layer = layers["global"]
    if not isinstance(global_layer, dict):
        raise ValueError("Cloud global overrides must be an object")
    if legacy_centralized(obj):
        for layer in global_layer.values():
            _config_layer(layer)
    else:
        _config_layer(global_layer)
    for scope in ("environments", "roles"):
        _named_layers(layers[scope])
    nodes = layers["nodes"]
    if not isinstance(nodes, dict) or not nodes:
        raise ValueError("Cloud topology requires at least one node assignment")
    repo_defaults = load_default_config() if repo_defaults is None else repo_defaults
    for node_id, node in nodes.items():
        if (not isinstance(node_id, str) or not node_id.strip()
                or not isinstance(node, dict)
                or set(node) != {"environment", "role", "overrides"}):
            raise ValueError("Invalid cloud node assignment")
        _config_layer(node["overrides"])
        for key, scope in (("environment", "environments"), ("role", "roles")):
            name = node[key]
            if name is not None and (not isinstance(name, str) or name not in layers[scope]):
                raise ValueError(f"Cloud node references an unknown {key}")
        defaults, overrides = resolve_layers(obj, node_id, repo_defaults)
        # Structural validation of every node does not require its local secrets.
        validator(merge_configs(defaults, overrides))


def resolve_layers(obj, node_id, repo_defaults=None):
    """Current release defaults precede cloud scopes; Settings edits only node."""
    layers = obj["layers"]
    if not centralized(obj):
        return copy.deepcopy(layers["defaults"]), copy.deepcopy(layers["overrides"])
    node = layers["nodes"].get(node_id)
    if node is None:
        raise ValueError("Local node_id has no assignment in cloud configuration")
    global_layer = layers["global"]
    if legacy_centralized(obj):
        # Iteration-2 sources stay frozen until explicitly reprovisioned.
        baseline = merge_configs(global_layer["defaults"], global_layer["overrides"])
    else:
        repo_defaults = load_default_config() if repo_defaults is None else repo_defaults
        baseline = merge_configs(repo_defaults, global_layer)
    for key, scope in (("environment", "environments"), ("role", "roles")):
        if node[key] is not None:
            baseline = merge_configs(baseline, layers[scope][node[key]])
    return copy.deepcopy(baseline), copy.deepcopy(node["overrides"])


def update_node_overrides(obj, node_id, config):
    updated = copy.deepcopy(obj)
    if centralized(obj):
        if node_id not in updated["layers"]["nodes"]:
            raise ValueError("Local node_id has no cloud assignment")
        updated["layers"]["nodes"][node_id]["overrides"] = copy.deepcopy(config)
    else:
        updated["layers"]["overrides"] = copy.deepcopy(config)
    return updated


def node_assignment(obj, node_id):
    if not centralized(obj):
        return {"environment": None, "role": None}
    node = obj["layers"]["nodes"][node_id]
    return {"environment": node["environment"], "role": node["role"]}


def initial_cloud_object(overrides, node_id):
    return {"schema_version": 1, "layers": {
        "global": {}, "environments": {}, "roles": {},
        "nodes": {node_id: {"environment": None, "role": None, "overrides": overrides}},
    }}
