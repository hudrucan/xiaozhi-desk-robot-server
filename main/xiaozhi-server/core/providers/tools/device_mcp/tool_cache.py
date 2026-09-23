"""Persistent device MCP tool inventory for single-device local deployments."""

import hashlib
import json
from pathlib import Path

from config.config_loader import get_project_dir
from core.utils.util import sanitize_tool_name


def device_mcp_cache_enabled(config):
    """Return whether the selected LLM can benefit from local prompt reuse."""
    cache_config = config.get("device_mcp_tool_cache", {})
    selected_name = config.get("selected_module", {}).get("LLM", "")
    selected_config = config.get("LLM", {}).get(selected_name, {})
    selected_type = selected_config.get("type", selected_name)
    return bool(cache_config.get("enabled", False) and selected_type == "llama_cpp")


def _canonical_schema(value):
    if isinstance(value, dict):
        return {
            key: _canonical_schema(value[key])
            for key in sorted(value)
        }
    if isinstance(value, list):
        return [_canonical_schema(item) for item in value]
    return value


def normalize_tool(tool):
    """Convert an MCP or OpenAI tool declaration to the shared LLM format."""
    if not isinstance(tool, dict):
        raise ValueError("Device MCP tool must be an object")

    function = tool.get("function")
    if isinstance(function, dict):
        name = function.get("name", "")
        description = function.get("description", "")
        parameters = function.get("parameters", {})
    else:
        name = tool.get("name", "")
        description = tool.get("description", "")
        parameters = tool.get("inputSchema", {})

    if not isinstance(name, str) or not name.strip():
        raise ValueError("Device MCP tool is missing a name")
    if not isinstance(description, str):
        description = str(description or "")
    if not isinstance(parameters, dict):
        parameters = {}

    properties = parameters.get("properties", {})
    if not isinstance(properties, dict):
        properties = {}
    required = parameters.get("required", [])
    if not isinstance(required, list):
        required = []

    return {
        "type": "function",
        "function": {
            "name": sanitize_tool_name(name),
            "description": description,
            "parameters": {
                "type": parameters.get("type", "object"),
                "properties": _canonical_schema(properties),
                "required": sorted(
                    item for item in required if isinstance(item, str)
                ),
            },
        },
    }


def normalize_inventory(tools):
    """Normalize tools while preserving their preferred presentation order."""
    if not isinstance(tools, list):
        raise ValueError("Device MCP tool inventory must be an array")
    return [normalize_tool(tool) for tool in tools]


def inventory_fingerprint(tools):
    """Return an order-independent fingerprint of the effective tool schemas."""
    normalized = sorted(
        normalize_inventory(tools),
        key=lambda tool: tool["function"]["name"],
    )
    payload = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _configured_paths(config):
    cache_config = config.get("device_mcp_tool_cache", {})
    project_dir = Path(get_project_dir())

    def resolve_path(value):
        path = Path(value).expanduser()
        return path if path.is_absolute() else project_dir / path

    runtime_path = resolve_path(
        cache_config.get("path", "data/.device_mcp_tools.json")
    )
    seed_path = resolve_path(
        cache_config.get("seed_path", "config/device_mcp_tools.json")
    )
    return cache_config, runtime_path, seed_path


def load_cached_inventory(config):
    """Load the learned inventory, falling back to the committed seed."""
    _, runtime_path, seed_path = _configured_paths(config)
    if not device_mcp_cache_enabled(config):
        return []

    for path in (runtime_path, seed_path):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return normalize_inventory(data)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return []


def save_cached_inventory(config, tools):
    """Atomically persist a firmware-confirmed inventory in data/."""
    _, runtime_path, _ = _configured_paths(config)
    if not device_mcp_cache_enabled(config):
        return

    normalized = normalize_inventory(tools)
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = runtime_path.with_suffix(runtime_path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(runtime_path)
