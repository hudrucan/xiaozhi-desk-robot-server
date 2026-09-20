"""External server MCP manager."""

import asyncio
import os
import json
from typing import Dict, Any, List

from mcp.types import LoggingMessageNotificationParams

from config.config_loader import get_project_dir
from config.logger import setup_logging
from .mcp_client import ServerMCPClient

TAG = __name__
logger = setup_logging()


class ServerMCPManager:
    """Manage configured external MCP servers."""

    def __init__(self, conn) -> None:
        """Initialize manager state without requiring a configuration file."""
        self.conn = conn
        self.config_path = get_project_dir() + "data/.mcp_server_settings.json"
        if not os.path.exists(self.config_path):
            self.config_path = ""
        self.clients: Dict[str, ServerMCPClient] = {}
        self.tools = []
        self._init_lock = asyncio.Lock()

    def load_config(self) -> Dict[str, Any]:
        """Load external MCP server configuration."""
        if len(self.config_path) == 0:
            return {}
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            return config.get("mcpServers", {})
        except Exception as e:
            logger.bind(tag=TAG).error(
                f"Error loading MCP config from {self.config_path}: {e}"
            )
            return {}

    def has_configured_servers(self) -> bool:
        """Return whether at least one usable external MCP server is configured."""
        return any(
            isinstance(server, dict)
            and (server.get("command") or server.get("url"))
            for server in self.load_config().values()
        )

    async def _init_server(self, name: str, srv_config: Dict[str, Any]):
        """Initialize one external MCP server."""
        client = None
        try:
            logger.bind(tag=TAG).info(f"Initializing external MCP client: {name}")
            client = ServerMCPClient(srv_config)
            # Limit initialization so one server cannot stall startup.
            await asyncio.wait_for(client.initialize(logging_callback=self.logging_callback), timeout=10)

            # Protect shared client/tool state.
            async with self._init_lock:
                self.clients[name] = client
                client_tools = client.get_available_tools()
                self.tools.extend(client_tools)

        except asyncio.TimeoutError:
            logger.bind(tag=TAG).error(
                f"Failed to initialize MCP server {name}: Timeout"
            )
            if client:
                await client.cleanup()
        except Exception as e:
            logger.bind(tag=TAG).error(
                f"Failed to initialize MCP server {name}: {e}"
            )
            if client:
                await client.cleanup()

    async def initialize_servers(self) -> None:
        """Initialize all configured external MCP servers."""
        config = self.load_config()
        tasks = []
        for name, srv_config in config.items():
            if not srv_config.get("command") and not srv_config.get("url"):
                logger.bind(tag=TAG).warning(
                    f"Skipping server {name}: neither command nor url specified"
                )
                continue
            
            tasks.append(self._init_server(name, srv_config))
        
        if tasks:
            await asyncio.gather(*tasks)

        # Refresh the tool inventory after discovery.
        if hasattr(self.conn, "func_handler") and self.conn.func_handler:
            if hasattr(self.conn.func_handler, "tool_manager"):
                self.conn.func_handler.tool_manager.refresh_tools()
            self.conn.func_handler.current_support_functions()

    def get_all_tools(self) -> List[Dict[str, Any]]:
        """Return tool declarations from all external MCP servers."""
        return self.tools

    def is_mcp_tool(self, tool_name: str) -> bool:
        """Return whether a name belongs to an external MCP tool."""
        for tool in self.tools:
            if (
                tool.get("function") is not None
                and tool["function"].get("name") == tool_name
            ):
                return True
        return False

    async def execute_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """Execute a tool call and reconnect before retrying failures."""
        logger.bind(tag=TAG).info(
            f"Executing external MCP tool {tool_name}, arguments: {arguments}"
        )

        max_retries = 3
        retry_interval = 2

        # Find the client that owns the tool.
        client_name = None
        target_client = None
        for name, client in self.clients.items():
            if client.has_tool(tool_name):
                client_name = name
                target_client = client
                break

        if not target_client:
            raise ValueError(f"Tool {tool_name} was not found on any MCP server")

        # Retry failed tool calls with a fresh client connection.
        for attempt in range(max_retries):
            try:
                return await target_client.call_tool(tool_name, arguments, progress_callback=self.progress_callback)
            except Exception as e:
                # Propagate the final failure.
                if attempt == max_retries - 1:
                    raise

                logger.bind(tag=TAG).warning(
                    f"Tool {tool_name} failed (attempt {attempt+1}/{max_retries}): {e}"
                )

                logger.bind(tag=TAG).info(
                    f"Reconnecting MCP client {client_name} before retry"
                )
                try:
                    # Close the old connection and recreate the client.
                    await target_client.cleanup()

                    config = self.load_config()
                    if client_name in config:
                        client = ServerMCPClient(config[client_name])
                        await client.initialize(logging_callback=self.logging_callback)
                        self.clients[client_name] = client
                        target_client = client
                        logger.bind(tag=TAG).info(
                            f"Reconnected MCP client: {client_name}"
                        )
                    else:
                        logger.bind(tag=TAG).error(
                            f"Cannot reconnect MCP client {client_name}: config not found"
                        )
                except Exception as reconnect_error:
                    logger.bind(tag=TAG).error(
                        f"Failed to reconnect MCP client {client_name}: {reconnect_error}"
                    )

                # Back off before retrying.
                await asyncio.sleep(retry_interval)

    async def cleanup_all(self) -> None:
        """Close all external MCP clients."""
        for name, client in list(self.clients.items()):
            try:
                if hasattr(client, "cleanup"):
                    await asyncio.wait_for(client.cleanup(), timeout=20)
                logger.bind(tag=TAG).info(f"External MCP client closed: {name}")
            except (asyncio.TimeoutError, Exception) as e:
                logger.bind(tag=TAG).error(f"Failed to close MCP client {name}: {e}")
        self.clients.clear()

    # Optional callbacks.

    async def logging_callback(self, params: LoggingMessageNotificationParams):
        logger.bind(tag=TAG).info(f"[Server Log - {params.level.upper()}] {params.data}")

    async def progress_callback(self, progress: float, total: float | None, message: str | None) -> None:
        logger.bind(tag=TAG).info(f"[Progress {progress}/{total}]: {message}")
