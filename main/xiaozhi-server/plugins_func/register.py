from config.logger import setup_logging
from enum import Enum

TAG = __name__

logger = setup_logging()


class ToolType(Enum):
    NONE = (1, "No follow-up action after tool execution")
    WAIT = (2, "Wait for the tool result")
    CHANGE_SYS_PROMPT = (3, "Update the system prompt")
    SYSTEM_CTL = (
        4,
        "System control that affects conversation flow and requires the connection",
    )
    IOT_CTL = (5, "IoT device control that requires the connection")
    MCP_CLIENT = (6, "MCP client")

    def __init__(self, code, message):
        self.code = code
        self.message = message


class Action(Enum):
    ERROR = (-1, "Error")
    NOTFOUND = (0, "Tool not found")
    NONE = (1, "No action")
    RESPONSE = (2, "Reply directly")
    REQLLM = (3, "Request an LLM reply after the tool result")
    RECORD = (4, "Record the tool call without another LLM request")

    def __init__(self, code, message):
        self.code = code
        self.message = message


class ActionResponse:
    def __init__(self, action: Action, result=None, response=None):
        self.action = action
        self.result = result
        self.response = response


class FunctionItem:
    def __init__(self, name, description, func, type):
        self.name = name
        self.description = description
        self.func = func
        self.type = type


class DeviceTypeRegistry:
    """Registry for IoT device types and their functions."""

    def __init__(self):
        self.type_functions = {}

    def generate_device_type_id(self, descriptor):
        """Build a stable type ID from a device capability descriptor."""
        properties = sorted(descriptor["properties"].keys())
        methods = sorted(descriptor["methods"].keys())
        # Properties and methods uniquely identify a device type.
        type_signature = (
            f"{descriptor['name']}:{','.join(properties)}:{','.join(methods)}"
        )
        return type_signature

    def get_device_functions(self, type_id):
        """Return functions registered for a device type."""
        return self.type_functions.get(type_id, {})

    def register_device_type(self, type_id, functions):
        """Register functions for a device type."""
        if type_id not in self.type_functions:
            self.type_functions[type_id] = functions


# Function registries.
all_function_registry = {}
module_func_map = {}


def register_function(name, desc, type=None):
    """Register a server function."""

    def decorator(func):
        all_function_registry[name] = FunctionItem(name, desc, func, type)
        # Map modules to functions so configuration may reference either form.
        module_name = func.__module__.split(".")[-1]
        module_func_map.setdefault(module_name, []).append(name)
        logger.bind(tag=TAG).debug(f"Function '{name}' loaded")
        return func

    return decorator


def register_device_function(name, desc, type=None):
    """Register a device-level function."""

    def decorator(func):
        logger.bind(tag=TAG).debug(f"Device function '{name}' loaded")
        return func

    return decorator


class FunctionRegistry:
    def __init__(self):
        self.function_registry = {}
        self.logger = setup_logging()

    def register_function(self, name, func_item=None):
        # Register a supplied function item directly.
        if func_item:
            self.function_registry[name] = func_item
            self.logger.bind(tag=TAG).debug(f"Function '{name}' registered directly")
            return func_item

        # Otherwise resolve it from the global registry.
        func = all_function_registry.get(name)
        if not func:
            self.logger.bind(tag=TAG).error(f"Function '{name}' not found")
            return None
        self.function_registry[name] = func
        self.logger.bind(tag=TAG).debug(f"Function '{name}' registered")
        return func

    def unregister_function(self, name):
        # Unregister an existing function.
        if name not in self.function_registry:
            self.logger.bind(tag=TAG).error(f"Function '{name}' not found")
            return False
        self.function_registry.pop(name, None)
        self.logger.bind(tag=TAG).info(f"Function '{name}' unregistered")
        return True

    def get_function(self, name):
        return self.function_registry.get(name)

    def get_all_functions(self):
        return self.function_registry

    def get_all_function_desc(self):
        return [func.description for _, func in self.function_registry.items()]
