"""Execute tools advertised by the connected firmware MCP server."""

from typing import Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from ..base import ToolType, ToolDefinition, ToolExecutor
from plugins_func.register import Action, ActionResponse
from core.utils.util import get_tool_error_response
from .mcp_handler import call_mcp_tool


class DeviceMCPExecutor(ToolExecutor):
    """Bridge unified tool calls to the connected firmware."""

    def __init__(self, conn):
        self.conn = conn

    async def execute(
        self,
        conn: "ConnectionHandler",
        tool_name: str,
        arguments: Dict[str, Any],
        diagnostic_call_id=None,
    ) -> ActionResponse:
        """Execute one firmware-owned MCP tool."""
        if not hasattr(conn, "mcp_client") or not conn.mcp_client:
            return ActionResponse(
                action=Action.ERROR,
                result="Device MCP client is not initialized",
                response=get_tool_error_response(conn.config),
            )

        if not await conn.mcp_client.is_ready():
            return ActionResponse(
                action=Action.ERROR,
                result="Device MCP client is not ready",
                response=get_tool_error_response(conn.config),
            )

        try:
            # Keep the existing wire helper compatible with string arguments.
            import json

            args_str = json.dumps(arguments) if arguments else "{}"

            result = await call_mcp_tool(
                conn,
                conn.mcp_client,
                tool_name,
                args_str,
                timeout=int(conn.config.get("tool_call_timeout", 30)),
                diagnostic_call_id=diagnostic_call_id,
            )

            resultJson = None
            if isinstance(result, str):
                try:
                    resultJson = json.loads(result)
                except Exception:
                    pass

            # Vision may return a complete user-facing response and skip the
            # second LLM request.
            if (
                resultJson is not None
                and isinstance(resultJson, dict)
                and "action" in resultJson
            ):
                return ActionResponse(
                    action=Action[resultJson["action"]],
                    response=resultJson.get("response", ""),
                )

            return ActionResponse(action=Action.REQLLM, result=str(result))

        except ValueError as e:
            return ActionResponse(
                action=Action.NOTFOUND,
                result=str(e),
                response=get_tool_error_response(conn.config),
            )
        except TimeoutError as e:
            return ActionResponse(
                action=Action.ERROR,
                result=str(e),
                response=get_tool_error_response(conn.config, timed_out=True),
            )
        except Exception as e:
            return ActionResponse(
                action=Action.ERROR,
                result=str(e),
                response=get_tool_error_response(conn.config),
            )

    def get_tools(self) -> Dict[str, ToolDefinition]:
        """Return tools advertised by the firmware."""
        if not hasattr(self.conn, "mcp_client") or not self.conn.mcp_client:
            return {}

        tools = {}
        mcp_tools = self.conn.mcp_client.get_available_tools()

        for tool in mcp_tools:
            func_def = tool.get("function", {})
            tool_name = func_def.get("name", "")

            if tool_name:
                tools[tool_name] = ToolDefinition(
                    name=tool_name, description=tool, tool_type=ToolType.DEVICE_MCP
                )

        return tools

    def has_tool(self, tool_name: str) -> bool:
        """Return whether the firmware advertised a tool."""
        if not hasattr(self.conn, "mcp_client") or not self.conn.mcp_client:
            return False

        return self.conn.mcp_client.has_tool(tool_name)
