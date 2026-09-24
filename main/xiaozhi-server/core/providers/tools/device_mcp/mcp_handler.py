"""Firmware MCP message and tool-call helpers."""

import json
import asyncio
import re
from concurrent.futures import Future
from core.utils.util import get_vision_url, sanitize_tool_name
from core.utils.auth import AuthToken
from config.logger import setup_logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()


class MCPClient:
    """Track firmware MCP state, tools, and pending calls."""

    def __init__(self):
        self.tools = {}  # sanitized_name -> tool_data
        self.name_mapping = {}
        self.ready = False
        self.call_results = {}  # To store Futures for tool call responses
        self.next_id = 1
        self.lock = asyncio.Lock()
        self._cached_available_tools = None  # Cache for get_available_tools

    def has_tool(self, name: str) -> bool:
        return name in self.tools

    def get_available_tools(self) -> list:
        # Check if the cache is valid
        if self._cached_available_tools is not None:
            return self._cached_available_tools

        # If cache is not valid, regenerate the list
        result = []
        for tool_name, tool_data in self.tools.items():
            function_def = {
                "name": tool_name,
                "description": tool_data["description"],
                "parameters": {
                    "type": tool_data["inputSchema"].get("type", "object"),
                    "properties": tool_data["inputSchema"].get("properties", {}),
                    "required": tool_data["inputSchema"].get("required", []),
                },
            }
            result.append({"type": "function", "function": function_def})

        self._cached_available_tools = result  # Store the generated list in cache
        return result

    async def is_ready(self) -> bool:
        async with self.lock:
            return self.ready

    async def set_ready(self, status: bool):
        async with self.lock:
            self.ready = status

    async def add_tool(self, tool_data: dict):
        async with self.lock:
            sanitized_name = sanitize_tool_name(tool_data["name"])
            self.tools[sanitized_name] = tool_data
            self.name_mapping[sanitized_name] = tool_data["name"]
            self._cached_available_tools = (
                None  # Invalidate the cache when a tool is added
            )

    async def get_next_id(self) -> int:
        async with self.lock:
            current_id = self.next_id
            self.next_id += 1
            return current_id

    async def register_call_result_future(self, id: int, future: Future):
        async with self.lock:
            self.call_results[id] = future

    async def resolve_call_result(self, id: int, result: any):
        async with self.lock:
            if id in self.call_results:
                future = self.call_results.pop(id)
                if not future.done():
                    future.set_result(result)

    async def reject_call_result(self, id: int, exception: Exception):
        async with self.lock:
            if id in self.call_results:
                future = self.call_results.pop(id)
                if not future.done():
                    future.set_exception(exception)

    async def cleanup_call_result(self, id: int):
        async with self.lock:
            if id in self.call_results:
                self.call_results.pop(id)


async def send_mcp_message(conn: "ConnectionHandler", payload: dict):
    """Helper to send MCP messages, encapsulating common logic."""
    if not conn.features.get("mcp"):
        logger.bind(tag=TAG).warning("Client does not support MCP; cannot send MCP message")
        return False

    message = json.dumps({"type": "mcp", "payload": payload})

    try:
        await conn.websocket.send(message)
        logger.bind(tag=TAG).debug(f"MCP message sent successfully: {message}")
        return True
    except Exception as e:
        logger.bind(tag=TAG).error(f"Failed to send MCP message: {e}")
        return False


async def handle_mcp_message(
    conn: "ConnectionHandler", mcp_client: MCPClient, payload: dict
):
    """Handle MCP initialization, inventory, calls, and responses."""
    logger.bind(tag=TAG).debug(f"Processing MCP message: {str(payload)[:100]}")

    if not isinstance(payload, dict):
        logger.bind(tag=TAG).error("MCP message is missing a payload or has an invalid format")
        return

    # Handle successful responses.
    if "result" in payload:
        result = payload["result"]
        msg_id = int(payload.get("id", 0))

        # Check for tool call response first
        if msg_id in mcp_client.call_results:
            logger.bind(tag=TAG).debug(
                f"Received tool-call response, ID: {msg_id}, result: {result}"
            )
            conn.mark_device_mcp_response(msg_id)
            await mcp_client.resolve_call_result(msg_id, result)
            return

        if msg_id == 1:  # mcpInitializeID
            logger.bind(tag=TAG).debug("Received MCP initialization response")
            server_info = result.get("serverInfo")
            if isinstance(server_info, dict):
                name = server_info.get("name")
                version = server_info.get("version")
                logger.bind(tag=TAG).debug(
                    f"Client MCP server information: name={name}, version={version}"
                )

            await asyncio.sleep(1)
            logger.bind(tag=TAG).debug("Initialization complete; requesting MCP tool list")
            await send_mcp_tools_list_request(conn)

            return

        elif msg_id == 2:  # mcpToolsListID
            logger.bind(tag=TAG).debug("Received MCP tool-list response")
            if isinstance(result, dict) and "tools" in result:
                tools_data = result["tools"]
                if not isinstance(tools_data, list):
                    logger.bind(tag=TAG).error("Invalid tool-list format")
                    return

                logger.bind(tag=TAG).info(
                    f"Number of tools supported by client device: {len(tools_data)}"
                )

                for i, tool in enumerate(tools_data):
                    if not isinstance(tool, dict):
                        continue

                    name = tool.get("name", "")
                    description = tool.get("description", "")
                    input_schema = {"type": "object", "properties": {}, "required": []}

                    if "inputSchema" in tool and isinstance(tool["inputSchema"], dict):
                        schema = tool["inputSchema"]
                        input_schema["type"] = schema.get("type", "object")
                        input_schema["properties"] = schema.get("properties", {})
                        input_schema["required"] = [
                            s for s in schema.get("required", []) if isinstance(s, str)
                        ]

                    new_tool = {
                        "name": name,
                        "description": description,
                        "inputSchema": input_schema,
                    }
                    mcp_client.pending_tools.append(new_tool)
                    logger.bind(tag=TAG).debug(f"Client tool #{i+1}: {name}")

                next_cursor = result.get("nextCursor", "")
                if next_cursor:
                    logger.bind(tag=TAG).debug(f"More tools are available, nextCursor: {next_cursor}")
                    await send_mcp_tools_list_continue_request(conn, next_cursor)
                else:
                    from .tool_cache import (
                        device_mcp_cache_enabled,
                        inventory_fingerprint,
                        normalize_inventory,
                        save_cached_inventory,
                    )

                    raw_tools = mcp_client.pending_tools
                    mcp_client.pending_tools = []
                    original_names = {
                        tool.get("name", "") for tool in raw_tools
                    }
                    # Match the server's runtime sanitization inside descriptions.
                    for tool in raw_tools:
                        description = tool.get("description", "")
                        for original_name in original_names:
                            if original_name:
                                description = description.replace(
                                    original_name, sanitize_tool_name(original_name)
                                )
                        tool["description"] = description

                    normalized_tools = normalize_inventory(raw_tools)

                    actual_fingerprint = inventory_fingerprint(normalized_tools)
                    cache_enabled = device_mcp_cache_enabled(conn.config)
                    cache_matches = (
                        cache_enabled
                        and mcp_client.cached_fingerprint == actual_fingerprint
                    )
                    await mcp_client.replace_tools(
                        raw_tools,
                        preserve_cached_order=cache_matches,
                    )
                    if cache_matches:
                        logger.bind(tag=TAG).info(
                            "Device MCP tool schemas match the preloaded inventory"
                        )
                    elif cache_enabled:
                        try:
                            save_cached_inventory(conn.config, normalized_tools)
                            logger.bind(tag=TAG).info(
                                "Device MCP tool schemas changed; updated the local cache"
                            )
                        except (OSError, ValueError) as e:
                            # The live firmware inventory remains usable even if
                            # its next-start optimization cannot be persisted.
                            logger.bind(tag=TAG).warning(
                                f"Unable to update the device MCP tool cache: {e}"
                            )
                        mcp_client.cached_fingerprint = actual_fingerprint

                    await mcp_client.set_ready(True)
                    logger.bind(tag=TAG).debug("All tools retrieved; MCP client is ready")

                    if getattr(conn, "func_handler", None):
                        # Initialization and hello run concurrently. Even when
                        # schemas match, refresh if the handler cached its tool
                        # list before hello installed the cached inventory.
                        needs_refresh = not cache_matches
                        if cache_matches and normalized_tools:
                            expected_tool = normalized_tools[0]["function"]["name"]
                            needs_refresh = not conn.func_handler.has_tool(
                                expected_tool
                            )
                        if needs_refresh:
                            conn.func_handler.tool_manager.refresh_tools()
                            conn.func_handler.current_support_functions()

                    pending_typed_input = getattr(
                        conn, "pending_typed_input", None
                    )
                    if pending_typed_input is not None:
                        from core.handle.receiveAudioHandle import (
                            process_pending_typed_input_if_ready,
                        )

                        await process_pending_typed_input_if_ready(conn)
                    else:
                        from core.handle.sendAudioHandle import send_status_message

                        await send_status_message(conn, "clear", "initializing")
            return

    # Handle method calls initiated by the client.
    elif "method" in payload:
        method = payload["method"]
        logger.bind(tag=TAG).info(f"Received MCP client request: {method}")

    elif "error" in payload:
        error_data = payload["error"]
        error_msg = error_data.get("message", "Unknown error")
        logger.bind(tag=TAG).error(f"Received MCP error response: {error_msg}")

        msg_id = int(payload.get("id", 0))
        if msg_id in mcp_client.call_results:
            conn.mark_device_mcp_response(msg_id, outcome="error")
            await mcp_client.reject_call_result(
                msg_id, Exception(f"MCP error: {error_msg}")
            )
        elif msg_id in (1, 2):
            from core.handle.sendAudioHandle import send_status_message

            await send_status_message(conn, "clear", "initializing")


async def send_mcp_initialize_message(conn: "ConnectionHandler"):
    """Send the MCP initialization request to firmware."""

    vision_url = get_vision_url(conn.config)

    # Generate the vision token from the server authentication key.
    auth = AuthToken(conn.config["server"]["auth_key"])
    token = auth.generate_token(conn.headers.get("device-id"))

    vision = {
        "url": vision_url,
        "token": token,
    }

    payload = {
        "jsonrpc": "2.0",
        "id": 1,  # mcpInitializeID
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "roots": {"listChanged": True},
                "sampling": {},
                "vision": vision,
            },
            "clientInfo": {
                "name": "XiaozhiClient",
                "version": "1.0.0",
            },
        },
    }
    logger.bind(tag=TAG).debug("Sending MCP initialization message")
    await send_mcp_message(conn, payload)


async def send_mcp_tools_list_request(conn: "ConnectionHandler"):
    """Request the first page of firmware tools."""
    payload = {
        "jsonrpc": "2.0",
        "id": 2,  # mcpToolsListID
        "method": "tools/list",
    }
    logger.bind(tag=TAG).debug("Sending MCP tool-list request")
    await send_mcp_message(conn, payload)


async def send_mcp_tools_list_continue_request(conn: "ConnectionHandler", cursor: str):
    """Request the next page of firmware tools."""
    payload = {
        "jsonrpc": "2.0",
        "id": 2,  # mcpToolsListID (same ID for continuation)
        "method": "tools/list",
        "params": {"cursor": cursor},
    }
    logger.bind(tag=TAG).info(f"Sending MCP tool-list request with cursor: {cursor}")
    await send_mcp_message(conn, payload)


async def call_mcp_tool(
    conn: "ConnectionHandler",
    mcp_client: MCPClient,
    tool_name: str,
    args: str = "{}",
    timeout: int = 30,
    diagnostic_call_id=None,
):
    """Call a firmware tool and wait for its correlated response."""
    if not await mcp_client.is_ready():
        raise RuntimeError("Device MCP client is not ready")

    if not mcp_client.has_tool(tool_name):
        raise ValueError(f"Tool {tool_name} does not exist")

    # Normalize the arguments before sending them to firmware.
    try:
        if isinstance(args, str):
            # Accept a JSON object encoded as a string.
            if not args.strip():
                arguments = {}
            else:
                try:
                    arguments = json.loads(args)
                except json.JSONDecodeError:
                    # Some models concatenate simple JSON objects; merge them
                    # only when each fragment can be parsed safely.
                    try:
                        # Extract flat JSON object fragments.
                        json_objects = re.findall(r"\{[^{}]*\}", args)
                        if len(json_objects) > 1:
                            # Merge fragments in their original order.
                            merged_dict = {}
                            for json_str in json_objects:
                                try:
                                    obj = json.loads(json_str)
                                    if isinstance(obj, dict):
                                        merged_dict.update(obj)
                                except json.JSONDecodeError:
                                    continue
                            if merged_dict:
                                arguments = merged_dict
                            else:
                                raise ValueError(
                                    f"Unable to parse a valid JSON object: {args}"
                                )
                        else:
                            raise ValueError(f"Unable to parse argument JSON: {args}")
                    except Exception as e:
                        logger.bind(tag=TAG).error(
                            f"Failed to parse argument JSON: {str(e)}, raw arguments: {args}"
                        )
                        raise ValueError(f"Unable to parse argument JSON: {str(e)}")
        elif isinstance(args, dict):
            arguments = args
        else:
            raise ValueError(
                f"Invalid argument type; expected string or object, got {type(args)}"
            )

        if not isinstance(arguments, dict):
            raise ValueError(f"Arguments must be an object, got {type(arguments)}")

    except Exception as e:
        if not isinstance(e, ValueError):
            raise ValueError(f"Failed to process arguments: {str(e)}")
        raise e

    tool_call_id = await mcp_client.get_next_id()
    result_future = asyncio.Future()
    await mcp_client.register_call_result_future(tool_call_id, result_future)

    actual_name = mcp_client.name_mapping.get(tool_name, tool_name)
    payload = {
        "jsonrpc": "2.0",
        "id": tool_call_id,
        "method": "tools/call",
        "params": {"name": actual_name, "arguments": arguments},
    }

    try:
        logger.bind(tag=TAG).info(
            f"Sending client MCP tool-call request: {actual_name}, arguments: {args}"
        )
        sent = await send_mcp_message(conn, payload)
        if sent:
            conn.mark_device_mcp_request(diagnostic_call_id, tool_call_id)
        # Wait for the matching firmware response or timeout.
        raw_result = await asyncio.wait_for(result_future, timeout=timeout)
        logger.bind(tag=TAG).info(
            f"Client MCP tool call {actual_name} succeeded, raw result: {raw_result}"
        )

        if isinstance(raw_result, dict):
            if raw_result.get("isError") is True:
                error_msg = raw_result.get(
                    "error", "Tool returned an error without details"
                )
                raise RuntimeError(f"Tool call failed: {error_msg}")

            content = raw_result.get("content")
            if isinstance(content, list) and len(content) > 0:
                if isinstance(content[0], dict) and "text" in content[0]:
                    # Keep text results intact for the LLM continuation.
                    return content[0]["text"]
        # Preserve unexpected result shapes as text for compatibility.
        return str(raw_result)
    except asyncio.TimeoutError as error:
        raise TimeoutError("Device MCP tool call timed out") from error
    finally:
        await mcp_client.cleanup_call_result(tool_call_id)
