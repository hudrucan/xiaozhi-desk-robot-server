"""设备端MCP客户端定义"""

import asyncio
import copy
from concurrent.futures import Future
from core.utils.util import sanitize_tool_name
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()


class MCPClient:
    """设备端MCP客户端，用于管理MCP状态和工具"""

    def __init__(self, cached_tools=None):
        self.tools = {}  # sanitized_name -> tool_data
        self.name_mapping = {}
        self.ready = False
        self.call_results = {}  # To store Futures for tool call responses
        self.next_id = 1
        self.lock = asyncio.Lock()
        self._cached_available_tools = None  # Cache for get_available_tools
        self.cached_fingerprint = None
        self.pending_tools = []
        if cached_tools:
            self.seed_cached_tools(cached_tools)

    def seed_cached_tools(self, tools):
        """Expose cached schemas for prompt construction, never execution."""
        from .tool_cache import inventory_fingerprint, normalize_inventory

        normalized = normalize_inventory(tools)
        self._cached_available_tools = normalized
        self.cached_fingerprint = inventory_fingerprint(normalized)
        self.tools = {
            tool["function"]["name"]: {
                "name": tool["function"]["name"],
                "description": tool["function"]["description"],
                "inputSchema": copy.deepcopy(tool["function"]["parameters"]),
            }
            for tool in normalized
        }
        self.name_mapping = {
            name: name for name in self.tools
        }

    async def replace_tools(self, raw_tools, preserve_cached_order=False):
        """Atomically install the firmware-confirmed inventory."""
        from .tool_cache import normalize_inventory

        normalized = normalize_inventory(raw_tools)
        actual_by_name = {}
        original_names = {}
        for raw_tool, llm_tool in zip(raw_tools, normalized):
            function = llm_tool["function"]
            sanitized_name = function["name"]
            original_name = raw_tool.get("name", sanitized_name)
            actual_by_name[sanitized_name] = {
                "name": original_name,
                "description": function["description"],
                "inputSchema": copy.deepcopy(function["parameters"]),
            }
            original_names[sanitized_name] = original_name

        async with self.lock:
            self.tools = actual_by_name
            self.name_mapping = original_names
            if not preserve_cached_order or self._cached_available_tools is None:
                self._cached_available_tools = normalized

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
