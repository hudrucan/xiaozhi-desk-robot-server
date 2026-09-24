"""External server MCP tool executor."""

from typing import Dict, Any, Optional
from ..base import ToolType, ToolDefinition, ToolExecutor
from plugins_func.register import Action, ActionResponse
from core.utils.util import get_tool_error_response
from .mcp_manager import ServerMCPManager


class ServerMCPExecutor(ToolExecutor):
    """Execute tools exposed by configured external MCP servers."""

    def __init__(self, conn):
        self.conn = conn
        self.mcp_manager: Optional[ServerMCPManager] = None
        self._initialized = False

    async def initialize(self):
        """Initialize configured external MCP servers, if any."""
        if not self._initialized:
            self._initialized = True
            manager = ServerMCPManager(self.conn)
            if not manager.has_configured_servers():
                return
            self.mcp_manager = manager
            await self.mcp_manager.initialize_servers()

    async def execute(
        self, conn, tool_name: str, arguments: Dict[str, Any]
    ) -> ActionResponse:
        """Execute an external MCP tool."""
        if not self._initialized or not self.mcp_manager:
            return ActionResponse(
                action=Action.ERROR,
                response="External MCP is not configured",
            )

        try:
            # Accept the legacy mcp_ prefix.
            actual_tool_name = tool_name
            if tool_name.startswith("mcp_"):
                actual_tool_name = tool_name[4:]

            result = await self.mcp_manager.execute_tool(actual_tool_name, arguments)

            return ActionResponse(action=Action.REQLLM, result=str(result))

        except ValueError as e:
            return ActionResponse(
                action=Action.NOTFOUND,
                result=str(e),
                response=get_tool_error_response(conn.config),
            )
        except Exception as e:
            return ActionResponse(
                action=Action.ERROR,
                result=str(e),
                response=get_tool_error_response(conn.config),
            )

    def get_tools(self) -> Dict[str, ToolDefinition]:
        """Return all discovered external MCP tools."""
        if not self._initialized or not self.mcp_manager:
            return {}

        tools = {}
        mcp_tools = self.mcp_manager.get_all_tools()

        for tool in mcp_tools:
            func_def = tool.get("function", {})
            tool_name = func_def.get("name", "")
            if tool_name == "":
                continue
            tools[tool_name] = ToolDefinition(
                name=tool_name, description=tool, tool_type=ToolType.SERVER_MCP
            )

        return tools

    def has_tool(self, tool_name: str) -> bool:
        """Return whether an external MCP tool is available."""
        if not self._initialized or not self.mcp_manager:
            return False

        # Accept the legacy mcp_ prefix.
        actual_tool_name = tool_name
        if tool_name.startswith("mcp_"):
            actual_tool_name = tool_name[4:]

        return self.mcp_manager.is_mcp_tool(actual_tool_name)

    async def cleanup(self):
        """Close external MCP connections."""
        if self.mcp_manager:
            await self.mcp_manager.cleanup_all()
