"""Manage override-only cloud topology through verified immutable objects and CAS."""

import argparse
import copy
import getpass
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1] / "main/xiaozhi-server"
sys.path.insert(0, str(PROJECT))

from config.bootstrap import load_bootstrap
from config.cloud_layers import centralized, legacy_centralized, shared_cluster
from config.cloud_secrets import LocalSecretStore
from config.config_store import ConfigConflict, ConfigUnavailable
from config.google_drive_config import GoogleDriveConfigStore


class TopologyError(ValueError):
    """Fixed diagnostics that do not include supplied configuration values."""


def topology_mutation(obj, operation, *, name=None, layer=None, environment=None, role=None,
                      current_node=None):
    if not centralized(obj) or legacy_centralized(obj):
        raise TopologyError("Topology administration requires an override-only source; explicitly reprovision legacy sources")
    layers = obj["layers"]
    if operation == "set-global":
        layers["global"] = copy.deepcopy(layer)
        return
    if operation == "set-cluster":
        if not shared_cluster(obj):
            raise TopologyError("Explicitly migrate centralized V1 before editing the shared cluster layer")
        layers["cluster"] = copy.deepcopy(layer)
        return
    if not isinstance(name, str) or not name.strip():
        raise TopologyError("Scope/node name must be a non-empty string")
    if operation in {"set-environment", "set-role", "delete-environment", "delete-role"}:
        scope = "environments" if operation.endswith("environment") else "roles"
        if operation.startswith("set-"):
            layers[scope][name] = copy.deepcopy(layer)
        else:
            if name not in layers[scope]:
                raise TopologyError("Scope does not exist")
            assignment = "environment" if scope == "environments" else "role"
            if any(node[assignment] == name for node in layers["nodes"].values()):
                raise TopologyError("Cannot delete a scope still referenced by a node")
            del layers[scope][name]
            # The common all-node validator rejects referenced scope deletion.
        return
    nodes = layers["nodes"]
    if operation == "add-node":
        if name in nodes:
            raise TopologyError("Node already exists")
        nodes[name] = {"environment": environment, "role": role,
                       "overrides": copy.deepcopy(layer if layer is not None else {})}
    elif operation == "set-node":
        if name not in nodes:
            raise TopologyError("Node does not exist")
        for key, value in (("environment", environment), ("role", role)):
            if value is not None:
                nodes[name][key] = None if value == "-" else value
        if layer is not None:
            nodes[name]["overrides"] = copy.deepcopy(layer)
    elif operation == "delete-node":
        if name == current_node:
            raise TopologyError("Cannot delete the local administering node")
        if name not in nodes:
            raise TopologyError("Node does not exist")
        if nodes[name]["environment"] is not None or nodes[name]["role"] is not None:
            raise TopologyError("Clear environment and role assignments before deleting a node")
        del nodes[name]
    else:
        raise TopologyError("Unknown topology operation")


def run_operation(store, operation, *, base_revision=None, **options):
    if operation == "migrate-cluster":
        return {"operation": operation, **store.migrate_cluster(
            options["nodes"], base_revision=base_revision, apply=options.get("apply", False))}
    if operation == "show":
        with store.locked():
            store.refresh_unlocked(strict=True)
            return {"operation": "show", "revision": store.revision_unlocked(),
                    "object": copy.deepcopy(store._desired_view()["payload"]["object"])}
    old, new = store.mutate(
        lambda obj: topology_mutation(obj, operation, current_node=store.bootstrap["node_id"], **options),
        base_revision=base_revision,
    )
    return {"operation": operation, "old_revision": old, "new_revision": new}


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootstrap", type=Path, help="Local bootstrap file (default: server data/bootstrap.yaml)")
    parser.add_argument("--base-revision", type=int, help="Optional revision previously inspected with show")
    operations = parser.add_subparsers(dest="operation", required=True)
    operations.add_parser("show")
    for name in ("set-global", "set-cluster"):
        command = operations.add_parser(name)
        command.add_argument("--file", type=Path, required=True, help="YAML/JSON reference-only override object replacing this scope")
    command = operations.add_parser("migrate-cluster", help="Preview/apply safe V1 node-to-cluster promotion")
    command.add_argument("--nodes", nargs="+", required=True)
    mode = command.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Preview only (default)")
    mode.add_argument("--apply", action="store_true", help="Publish using the inspected --base-revision")
    for scope in ("environment", "role"):
        command = operations.add_parser("set-" + scope)
        command.add_argument("name")
        command.add_argument("--file", type=Path, required=True)
        command = operations.add_parser("delete-" + scope)
        command.add_argument("name")
    for operation in ("add-node", "set-node", "delete-node"):
        command = operations.add_parser(operation)
        command.add_argument("name", help="Node identity")
        if operation != "delete-node":
            command.add_argument("--environment", help="Assignment; '-' clears an existing assignment")
            command.add_argument("--role", help="Assignment; '-' clears an existing assignment")
            command.add_argument("--file", type=Path, help="Replace only this node's overrides")
    command = operations.add_parser("set-local-secret", help="Store a named reference on this node only")
    command.add_argument("name")
    return parser


def main(argv=None, store_factory=GoogleDriveConfigStore):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        bootstrap = load_bootstrap(args.bootstrap)
        if args.operation == "set-local-secret":
            if not sys.stdin.isatty():
                print("Secret input requires an interactive terminal.", file=sys.stderr)
                return 2
            LocalSecretStore(bootstrap["node_id"]).put_many({args.name: getpass.getpass("Secret value: ")})
            print("Stored secret locally; cloud source and provider selection unchanged.")
            return 0
        options = {}
        if args.operation == "migrate-cluster":
            options.update(nodes=args.nodes, apply=args.apply)
        for key in ("name", "environment", "role"):
            value = getattr(args, key, None)
            if args.operation == "add-node" and value == "-" and key != "name":
                value = None
            options[key] = value
        if getattr(args, "file", None) is not None:
            import yaml
            # Do not include input/parser errors or values in CLI diagnostics.
            try:
                options["layer"] = yaml.safe_load(args.file.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError):
                raise ValueError("Cannot read override file") from None
            if not isinstance(options["layer"], dict):
                raise ValueError("Override file must contain an object")
        store = store_factory({**bootstrap, "config_provider": "google_drive"})
        result = run_operation(store, args.operation, base_revision=args.base_revision, **options)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ConfigConflict:
        print("Conflict: cloud revision changed; read and review before retrying.", file=sys.stderr)
        return 3
    except ConfigUnavailable:
        print("Cloud unavailable; sync and confirm the revision before retrying.", file=sys.stderr)
        return 4
    except TopologyError as error:
        print(str(error), file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, KeyError):
        print("Invalid configuration, assignment, or local secret storage; no overwrite was forced.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
