"""Server plugin tool executor."""

import asyncio
from typing import Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from ..base import ToolType, ToolDefinition, ToolExecutor
from plugins_func.register import all_function_registry, module_func_map, Action, ActionResponse


class ServerPluginExecutor(ToolExecutor):
    """Execute server-side plugin tools."""

    def __init__(self, conn: "ConnectionHandler"):
        self.conn = conn
        self.config = conn.config

    async def execute(
        self, conn: "ConnectionHandler", tool_name: str, arguments: Dict[str, Any]
    ) -> ActionResponse:
        """Execute a server-side plugin tool."""
        func_item = all_function_registry.get(tool_name)
        if not func_item:
            return ActionResponse(
                action=Action.NOTFOUND, response=f"Plugin function {tool_name} not found"
            )

        try:
            if hasattr(func_item, "type"):
                func_type = func_item.type
                if func_type.code in [4, 5]:
                    result = func_item.func(conn, **arguments)
                elif func_type.code == 2:
                    result = func_item.func(**arguments)
                elif func_type.code == 3:
                    result = func_item.func(conn, **arguments)
                else:
                    result = func_item.func(**arguments)
            else:
                result = func_item.func(**arguments)

            if asyncio.iscoroutine(result):
                result = await result

            return result

        except Exception as e:
            return ActionResponse(
                action=Action.ERROR,
                response=str(e),
            )

    def _expand_plugin_names(self, config_functions):
        """Expand module-level plugin names into registered function names.

        One plugin module may register multiple functions, while configuration may
        refer to either the module or an individual function.
        """
        if not isinstance(config_functions, list):
            try:
                config_functions = list(config_functions)
            except TypeError:
                return []

        expanded = []
        for name in config_functions:
            if name in module_func_map:
                expanded.extend(module_func_map[name])
            elif name in all_function_registry:
                expanded.append(name)
            else:
                expanded.append(name)
        return expanded

    def _get_plugin_description(self, func_name):
        """Return a configured description for a function or its module."""
        plugins = self.config.get("plugins", {})
        if func_name in plugins:
            return plugins[func_name].get("description", "")
        for module_name, func_names in module_func_map.items():
            if func_name in func_names and module_name in plugins:
                return plugins[module_name].get("description", "")
        return ""

    @staticmethod
    def _has_valid_value(value):
        if value is None:
            return False
        normalized = str(value).strip().lower()
        if not normalized:
            return False
        return not any(
            marker in normalized
            for marker in ("your_", "your ", "placeholder", "xxx", "你的")
        )

    def _is_configured(self, func_name):
        plugins = self.config.get("plugins", {})
        plugin_config = plugins.get(func_name, {})
        if func_name == "web_search":
            return (
                str(plugin_config.get("provider", "")).lower()
                in {"metaso", "tavily"}
                and self._has_valid_value(plugin_config.get("api_key"))
            )
        if func_name == "get_weather":
            return self._has_valid_value(
                plugin_config.get("api_host")
            ) and self._has_valid_value(plugin_config.get("api_key"))
        return True

    def get_tools(self) -> Dict[str, ToolDefinition]:
        """Return configured server plugin tools."""
        tools = {}

        necessary_functions = ["handle_exit_intent", "get_lunar"]

        config_functions = self.config["Intent"][
            self.config["selected_module"]["Intent"]
        ].get("functions", [])

        config_functions = self._expand_plugin_names(config_functions)
        all_required_functions = list(
            dict.fromkeys(necessary_functions + config_functions)
        )

        for func_name in all_required_functions:
            func_item = all_function_registry.get(func_name)
            if func_item and self._is_configured(func_name):
                fun_description = self._get_plugin_description(func_name)
                if fun_description is not None and len(fun_description) > 0:
                    if "function" in func_item.description and isinstance(
                        func_item.description["function"], dict
                    ):
                        func_item.description["function"][
                            "description"
                        ] = fun_description

                tools[func_name] = ToolDefinition(
                    name=func_name,
                    description=func_item.description,
                    tool_type=ToolType.SERVER_PLUGIN,
                )

        return tools

    def has_tool(self, tool_name: str) -> bool:
        """Return whether a configured server plugin tool is available."""
        return tool_name in all_function_registry and self._is_configured(tool_name)
