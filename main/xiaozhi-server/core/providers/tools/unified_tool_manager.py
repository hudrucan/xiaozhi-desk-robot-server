"""Unified tool manager."""

from typing import Dict, List, Optional, Any
from config.logger import setup_logging
from plugins_func.register import Action, ActionResponse
from core.utils.util import get_tool_error_response
from .base import ToolType, ToolDefinition, ToolExecutor


class ToolManager:
    """Manage tool declarations and executors across all tool types."""

    def __init__(self, conn):
        self.conn = conn
        self.logger = setup_logging()
        self.executors: Dict[ToolType, ToolExecutor] = {}
        self._cached_tools: Optional[Dict[str, ToolDefinition]] = None
        self._cached_function_descriptions: Optional[List[Dict[str, Any]]] = None

    def register_executor(self, tool_type: ToolType, executor: ToolExecutor):
        """Register an executor for a tool type."""
        self.executors[tool_type] = executor
        self._invalidate_cache()
        self.logger.debug(f"Tool executor registered: {tool_type.value}")

    def _invalidate_cache(self):
        """Invalidate cached tool declarations."""
        self._cached_tools = None
        self._cached_function_descriptions = None

    def get_all_tools(self) -> Dict[str, ToolDefinition]:
        """Return all available tool definitions."""
        if self._cached_tools is not None:
            return self._cached_tools

        all_tools = {}
        for tool_type, executor in self.executors.items():
            try:
                tools = executor.get_tools()
                for name, definition in tools.items():
                    if name in all_tools:
                        self.logger.warning(f"Tool name conflict: {name}")
                    all_tools[name] = definition
            except Exception as e:
                self.logger.error(f"Failed to load {tool_type.value} tools: {e}")

        self._cached_tools = all_tools
        return all_tools

    def get_function_descriptions(self) -> List[Dict[str, Any]]:
        """Return all tool descriptions in the shared function format."""
        if self._cached_function_descriptions is not None:
            return self._cached_function_descriptions

        descriptions = []
        tools = self.get_all_tools()
        for tool_definition in tools.values():
            descriptions.append(tool_definition.description)

        self._cached_function_descriptions = descriptions
        return descriptions

    def has_tool(self, tool_name: str) -> bool:
        """Return whether a tool exists."""
        tools = self.get_all_tools()
        return tool_name in tools

    def get_tool_type(self, tool_name: str) -> Optional[ToolType]:
        """Return the type of a tool."""
        tools = self.get_all_tools()
        tool_def = tools.get(tool_name)
        return tool_def.tool_type if tool_def else None

    async def execute_tool(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> ActionResponse:
        """Execute a tool call."""
        try:
            # Resolve the owning executor.
            tool_type = self.get_tool_type(tool_name)
            if not tool_type:
                return ActionResponse(
                    action=Action.NOTFOUND,
                    result=f"Tool {tool_name} does not exist",
                    response=get_tool_error_response(self.conn.config),
                )

            executor = self.executors.get(tool_type)
            if not executor:
                return ActionResponse(
                    action=Action.ERROR,
                    result=f"No executor is registered for {tool_type.value}",
                    response=get_tool_error_response(self.conn.config),
                )

            self.logger.info(f"Executing tool: {tool_name}, arguments: {arguments}")
            result = await executor.execute(self.conn, tool_name, arguments)
            self.logger.debug(f"Tool result: {result}")
            return result

        except Exception as e:
            self.logger.error(f"Tool {tool_name} failed: {e}")
            return ActionResponse(
                action=Action.ERROR,
                result=str(e),
                response=get_tool_error_response(self.conn.config),
            )

    def get_supported_tool_names(self) -> List[str]:
        """Return all supported tool names."""
        tools = self.get_all_tools()
        return list(tools.keys())

    def refresh_tools(self):
        """Refresh the tool cache."""
        self._invalidate_cache()
        self.logger.debug("Tool cache refreshed")

    def get_tool_statistics(self) -> Dict[str, int]:
        """Return tool counts by type."""
        stats = {}
        for tool_type, executor in self.executors.items():
            try:
                tools = executor.get_tools()
                stats[tool_type.value] = len(tools)
            except Exception as e:
                self.logger.error(f"Failed to count {tool_type.value} tools: {e}")
                stats[tool_type.value] = 0
        return stats
