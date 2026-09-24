"""Unified tool handler."""

import json
from typing import Dict, List, Any, Optional
from config.logger import setup_logging
from plugins_func.loadplugins import auto_import_modules

from .base import ToolType
from plugins_func.register import Action, ActionResponse
from .unified_tool_manager import ToolManager
from .server_plugins import ServerPluginExecutor
from .server_mcp import ServerMCPExecutor
from .device_iot import DeviceIoTExecutor
from .device_mcp import DeviceMCPExecutor
from .mcp_endpoint import MCPEndpointExecutor
from core.handle.sendAudioHandle import send_display_message
from core.utils.util import get_tool_error_response


class UnifiedToolHandler:
    """Coordinate server, external MCP, and device tools."""

    def __init__(self, conn):
        self.conn = conn
        self.config = conn.config
        self.logger = setup_logging()

        # Create the tool manager and executors.
        self.tool_manager = ToolManager(conn)

        self.server_plugin_executor = ServerPluginExecutor(conn)
        self.server_mcp_executor = ServerMCPExecutor(conn)
        self.device_iot_executor = DeviceIoTExecutor(conn)
        self.device_mcp_executor = DeviceMCPExecutor(conn)
        self.mcp_endpoint_executor = MCPEndpointExecutor(conn)

        # Register executors by tool type.
        self.tool_manager.register_executor(
            ToolType.SERVER_PLUGIN, self.server_plugin_executor
        )
        self.tool_manager.register_executor(
            ToolType.SERVER_MCP, self.server_mcp_executor
        )
        self.tool_manager.register_executor(
            ToolType.DEVICE_IOT, self.device_iot_executor
        )
        self.tool_manager.register_executor(
            ToolType.DEVICE_MCP, self.device_mcp_executor
        )
        self.tool_manager.register_executor(
            ToolType.MCP_ENDPOINT, self.mcp_endpoint_executor
        )

        # Initialization state.
        self.finish_init = False

    async def _initialize(self):
        """Initialize configured tool sources."""
        try:
            # Import registered server plugins.
            auto_import_modules("plugins_func.functions")

            # External server MCP stays inactive when no server is configured.
            await self.server_mcp_executor.initialize()

            # Initialize the optional MCP endpoint.
            await self._initialize_mcp_endpoint()

            self.finish_init = True
            self.logger.debug("Unified tool handler initialized")

            # Log the resulting tool inventory.
            self.current_support_functions()

        except Exception as e:
            self.logger.error(f"Failed to initialize unified tool handler: {e}")

    async def _initialize_mcp_endpoint(self):
        """Initialize the optional MCP endpoint."""
        try:
            from .mcp_endpoint import connect_mcp_endpoint

            # Read the endpoint URL from local configuration.
            mcp_endpoint_url = self.config.get("mcp_endpoint", "")

            if (
                mcp_endpoint_url
                and "你的" not in mcp_endpoint_url
                and mcp_endpoint_url != "null"
            ):
                self.logger.info(f"Initializing MCP endpoint: {mcp_endpoint_url}")
                mcp_endpoint_client = await connect_mcp_endpoint(
                    mcp_endpoint_url, self.conn
                )

                if mcp_endpoint_client:
                    # Keep the client on the connection for lifecycle cleanup.
                    self.conn.mcp_endpoint_client = mcp_endpoint_client
                    self.logger.info("MCP endpoint initialized")
                else:
                    self.logger.warning("MCP endpoint initialization failed")

        except Exception as e:
            self.logger.error(f"Failed to initialize MCP endpoint: {e}")

    def get_functions(self) -> List[Dict[str, Any]]:
        """Return all available tool declarations."""
        return self.tool_manager.get_function_descriptions()

    def current_support_functions(self) -> List[str]:
        """Return the names of currently available tools."""
        func_names = self.tool_manager.get_supported_tool_names()
        self.logger.info(f"Available tools: {func_names}")
        return func_names

    def upload_functions_desc(self):
        """Refresh the cached tool declarations."""
        self.tool_manager.refresh_tools()
        self.logger.info("Tool declarations refreshed")

    def has_tool(self, tool_name: str) -> bool:
        """Return whether a tool is available."""
        return self.tool_manager.has_tool(tool_name)

    async def handle_llm_function_call(
        self, conn, function_call_data: Dict[str, Any]
    ) -> Optional[ActionResponse]:
        """Handle a function call emitted by the LLM."""
        try:
            # Handle multiple function calls in order.
            if "function_calls" in function_call_data:
                responses = []
                for call in function_call_data["function_calls"]:
                    result = await self.tool_manager.execute_tool(
                        call["name"],
                        call.get("arguments", {}),
                        diagnostic_call_id=call.get("id"),
                    )
                    responses.append(result)
                return self._combine_responses(responses)

            # Handle one function call.
            function_name = function_call_data["name"]
            arguments = function_call_data.get("arguments", {})

            # Parse string-encoded JSON arguments.
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments) if arguments else {}
                except json.JSONDecodeError:
                    self.logger.error(f"Unable to parse function arguments: {arguments}")
                    return ActionResponse(
                        action=Action.ERROR,
                        result="Unable to parse function arguments",
                        response=get_tool_error_response(self.config),
                    )

            self.logger.debug(f"Calling tool: {function_name}, arguments: {arguments}")

            # Show the tool call on the device.
            try:
                await send_display_message(self.conn, f"% {function_name}")
            except Exception as e:
                self.logger.warning(f"Failed to send tool-call display message: {e}")

            # Execute the tool call.
            result = await self.tool_manager.execute_tool(
                function_name,
                arguments,
                diagnostic_call_id=function_call_data.get("id"),
            )
            return result

        except Exception as e:
            self.logger.error(f"Function-call handling failed: {e}")
            return ActionResponse(
                action=Action.ERROR,
                result=str(e),
                response=get_tool_error_response(self.config),
            )

    def _combine_responses(self, responses: List[ActionResponse]) -> ActionResponse:
        """Combine responses from multiple tool calls."""
        if not responses:
            return ActionResponse(action=Action.NONE, response="No response")

        # Return the first error.
        for response in responses:
            if response.action == Action.ERROR:
                return response

        # Combine successful responses.
        contents = []
        responses_text = []

        for response in responses:
            if response.content:
                contents.append(response.content)
            if response.response:
                responses_text.append(response.response)

        # Determine the final action.
        final_action = Action.RESPONSE
        for response in responses:
            if response.action == Action.REQLLM:
                final_action = Action.REQLLM
                break

        return ActionResponse(
            action=final_action,
            result="; ".join(contents) if contents else None,
            response="; ".join(responses_text) if responses_text else None,
        )

    async def register_iot_tools(self, descriptors: List[Dict[str, Any]]):
        """Register device IoT tools."""
        self.device_iot_executor.register_iot_tools(descriptors)
        self.tool_manager.refresh_tools()
        self.logger.info(f"Registered {len(descriptors)} device IoT tools")

    def get_tool_statistics(self) -> Dict[str, int]:
        """Return tool statistics."""
        return self.tool_manager.get_tool_statistics()

    async def cleanup(self):
        """Clean up tool resources."""
        try:
            await self.server_mcp_executor.cleanup()

            # Close the MCP endpoint connection.
            if (
                hasattr(self.conn, "mcp_endpoint_client")
                and self.conn.mcp_endpoint_client
            ):
                await self.conn.mcp_endpoint_client.close()

            self.logger.info("Tool handler cleanup completed")
        except Exception as e:
            self.logger.error(f"Tool handler cleanup failed: {e}")
