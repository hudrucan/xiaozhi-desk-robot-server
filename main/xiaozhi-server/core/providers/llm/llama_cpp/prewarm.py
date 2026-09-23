"""Build the stable llama.cpp prompt prefix shared by server and benchmarks."""

from types import SimpleNamespace

from core.providers.tools.device_mcp.tool_cache import (
    device_mcp_cache_enabled,
    load_cached_inventory,
)
from core.providers.tools.server_plugins import ServerPluginExecutor
from core.utils.prompt_manager import PromptManager
from plugins_func.loadplugins import auto_import_modules


def build_prewarm_request(config, device_id="preloaded-device", logger=None):
    """Return the system prompt and tools used for llama.cpp prewarming."""
    cache_config = config.get("device_mcp_tool_cache", {})
    selected_intent = config.get("selected_module", {}).get("Intent", "")
    intent_type = config.get("Intent", {}).get(selected_intent, {}).get("type")
    if (
        not device_mcp_cache_enabled(config)
        or not cache_config.get("prewarm", True)
        or intent_type != "function_call"
    ):
        return None

    device_tools = load_cached_inventory(config)
    if not device_tools:
        return None

    auto_import_modules("plugins_func.functions")
    proxy = SimpleNamespace(config=config)
    plugin_tools = ServerPluginExecutor(proxy).get_tools()
    functions = [
        definition.description for definition in plugin_tools.values()
    ] + device_tools
    system_prompt = PromptManager(config, logger).build_enhanced_prompt(
        config.get("prompt", ""),
        device_id,
        emoji_enabled=True,
    )
    return system_prompt, functions
