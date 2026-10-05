"""Cloud topology: V1 compatibility and V2 shared cluster desired state."""

import copy
from collections.abc import Mapping

from config.config_loader import load_default_config, merge_configs
from config.cloud_secrets import validate_cloud_secrets

BOOTSTRAP_ROOTS = {"node_id", "config_provider", "google_drive", "credentials_path"}
LEGACY_LAYERS = {"defaults", "overrides"}
CENTRAL_LAYERS = {"global", "environments", "roles", "nodes"}
SHARED_LAYERS = CENTRAL_LAYERS | {"cluster"}


def _without_inherited_asset_pointers(baseline, overrides):
    """An explicit file owner cannot inherit the previous owner's cloud pointer."""
    baseline = copy.deepcopy(baseline)
    inherited = baseline.get("static_soundbank", {})
    narrower = overrides.get("static_soundbank", {})
    if not isinstance(inherited, Mapping) or not isinstance(narrower, Mapping):
        return baseline
    inherited_entries = inherited.get("entries", {})
    entries = narrower.get("entries", {})
    if not isinstance(inherited_entries, Mapping) or not isinstance(entries, Mapping):
        return baseline
    for phrase, entry in entries.items():
        previous = inherited_entries.get(phrase)
        if not isinstance(entry, Mapping) or not isinstance(previous, Mapping):
            continue  # Scalar replacement already discards all inherited metadata.
        if "file" in entry:
            previous.pop("cloud", None)
        optimized = entry.get("optimized")
        previous_optimized = previous.get("optimized")
        if (isinstance(optimized, Mapping) and "file" in optimized
                and isinstance(previous_optimized, Mapping)):
            previous_optimized.pop("cloud", None)
    return baseline


def merge_cloud_configs(baseline, overrides):
    """Cloud-only merge; other fields retain the generic recursive/scalar rules."""
    return merge_configs(_without_inherited_asset_pointers(baseline, overrides), overrides)


def centralized(obj):
    return set(obj["layers"]) in (CENTRAL_LAYERS, SHARED_LAYERS)


def shared_cluster(obj):
    return obj.get("schema_version") == 2 and set(obj["layers"]) == SHARED_LAYERS


def legacy_centralized(obj):
    return set(obj["layers"]) == CENTRAL_LAYERS and set(obj["layers"]["global"]) == LEGACY_LAYERS


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
    if type(obj.get("schema_version")) is not int or (
        obj["schema_version"] == 2 and set(layers) != SHARED_LAYERS
        or obj["schema_version"] == 1 and set(layers) not in (LEGACY_LAYERS, CENTRAL_LAYERS)
        or obj["schema_version"] not in (1, 2)
    ):
        raise ValueError("Cloud schema version and scopes do not match")
    if set(layers) == LEGACY_LAYERS:
        for layer in layers.values():
            _config_layer(layer)
        validator(merge_cloud_configs(layers["defaults"], layers["overrides"]))
        return
    if set(layers) not in (CENTRAL_LAYERS, SHARED_LAYERS):
        raise ValueError("Invalid cloud configuration scopes")
    global_layer = layers["global"]
    if not isinstance(global_layer, dict):
        raise ValueError("Cloud global overrides must be an object")
    if legacy_centralized(obj):
        for layer in global_layer.values():
            _config_layer(layer)
    else:
        if shared_cluster(obj) and set(global_layer) == LEGACY_LAYERS:
            raise ValueError("Shared V2 global must be overrides, not frozen legacy defaults")
        _config_layer(global_layer)
    for scope in ("environments", "roles"):
        _named_layers(layers[scope])
    if shared_cluster(obj):
        _config_layer(layers["cluster"])
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
    """Defaults < global < environment < role < cluster < node exceptions."""
    layers = obj["layers"]
    if not centralized(obj):
        return (_without_inherited_asset_pointers(layers["defaults"], layers["overrides"]),
                copy.deepcopy(layers["overrides"]))
    node = layers["nodes"].get(node_id)
    if node is None:
        raise ValueError("Local node_id has no assignment in cloud configuration")
    global_layer = layers["global"]
    if legacy_centralized(obj):
        # Iteration-2 sources stay frozen until explicitly reprovisioned.
        baseline = merge_cloud_configs(global_layer["defaults"], global_layer["overrides"])
    else:
        repo_defaults = load_default_config() if repo_defaults is None else repo_defaults
        baseline = merge_cloud_configs(repo_defaults, global_layer)
    for key, scope in (("environment", "environments"), ("role", "roles")):
        if node[key] is not None:
            baseline = merge_cloud_configs(baseline, layers[scope][node[key]])
    if shared_cluster(obj):
        baseline = merge_cloud_configs(baseline, layers["cluster"])
    # Keep raw node overrides for editing/publication, while making even callers
    # that merge the returned pair generically see the same dependent metadata.
    return (_without_inherited_asset_pointers(baseline, node["overrides"]),
            copy.deepcopy(node["overrides"]))


def update_node_overrides(obj, node_id, config):
    updated = copy.deepcopy(obj)
    if centralized(obj):
        if node_id not in updated["layers"]["nodes"]:
            raise ValueError("Local node_id has no cloud assignment")
        updated["layers"]["nodes"][node_id]["overrides"] = copy.deepcopy(config)
    else:
        updated["layers"]["overrides"] = copy.deepcopy(config)
    return updated


def update_settings_overrides(obj, node_id, config):
    """V2 Settings owns cluster; historical sources keep their original rules."""
    if not shared_cluster(obj):
        return update_node_overrides(obj, node_id, config)
    updated = copy.deepcopy(obj)
    if node_id not in updated["layers"]["nodes"]:
        raise ValueError("Local node_id has no cloud assignment")
    updated["layers"]["cluster"] = copy.deepcopy(config)
    return updated


def migrate_cluster_object(obj, node_ids, validator, repo_defaults):
    """Pure preview: promote identical raw overrides, proving ALL node views."""
    from config.config_store import canonical_bytes

    validate_layers(obj, validator, repo_defaults)
    if not centralized(obj) or legacy_centralized(obj):
        raise ValueError("Cluster migration requires override-only centralized V1 or shared V2")
    if (not isinstance(node_ids, (list, tuple)) or not node_ids
            or any(not isinstance(node, str) or node not in obj["layers"]["nodes"] for node in node_ids)
            or len(set(node_ids)) != len(node_ids)):
        raise ValueError("Select distinct existing nodes for cluster migration")
    if shared_cluster(obj):
        # An explicit node exception introduced after migration must stay intact.
        return copy.deepcopy(obj)
    common = obj["layers"]["nodes"][node_ids[0]]["overrides"]
    if any(canonical_bytes(obj["layers"]["nodes"][node]["overrides"]) != canonical_bytes(common)
           for node in node_ids):
        raise ValueError("Participating node overrides differ; cluster migration rejected")
    updated = copy.deepcopy(obj)
    updated["schema_version"] = 2
    updated["layers"]["cluster"] = copy.deepcopy(common)
    for node in node_ids:
        updated["layers"]["nodes"][node]["overrides"] = {}
    validate_layers(updated, validator, repo_defaults)
    # A cluster layer also affects unselected nodes. Never compensate by silently
    # rewriting their exceptions; reject if any node's resolved reference view changes.
    for node in obj["layers"]["nodes"]:
        before = merge_configs(*resolve_layers(obj, node, repo_defaults))
        after = merge_configs(*resolve_layers(updated, node, repo_defaults))
        if canonical_bytes(before) != canonical_bytes(after):
            raise ValueError("Migration changes an effective node configuration; migration rejected")
    return updated


def node_assignment(obj, node_id):
    if not centralized(obj):
        return {"environment": None, "role": None}
    node = obj["layers"]["nodes"][node_id]
    return {"environment": node["environment"], "role": node["role"]}


def initial_cloud_object(overrides, node_id):
    """Historical V1 constructor, retained for compatibility and migration."""
    return {"schema_version": 1, "layers": {
        "global": {}, "environments": {}, "roles": {},
        "nodes": {node_id: {"environment": None, "role": None, "overrides": overrides}},
    }}


def initial_cluster_object(overrides, node_id):
    """New sources share normal configuration; nodes hold only assignments."""
    return {"schema_version": 2, "layers": {
        "global": {}, "environments": {}, "roles": {}, "cluster": copy.deepcopy(overrides),
        "nodes": {node_id: {"environment": None, "role": None, "overrides": {}}},
    }}
