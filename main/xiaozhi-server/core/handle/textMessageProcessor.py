import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from core.handle.textMessageHandlerRegistry import TextMessageHandlerRegistry

TAG = __name__


class TextMessageProcessor:
    """Dispatch incoming text messages to their registered handlers."""

    def __init__(self, registry: TextMessageHandlerRegistry):
        self.registry = registry

    async def process_message(self, conn: "ConnectionHandler", message: str) -> None:
        """Process one incoming text message."""
        try:
            # Parse the JSON message.
            msg_json = json.loads(message)

            # Handle JSON messages.
            if isinstance(msg_json, dict):
                message_type = msg_json.get("type")

                message_logger = conn.logger.bind(tag=TAG)
                if message_type == "mcp":
                    message_logger.debug(f"Received {message_type} message: {message}")
                else:
                    message_logger.info(f"Received {message_type} message: {message}")

                # Resolve and invoke the registered handler.
                handler = self.registry.get_handler(message_type)
                if handler:
                    await handler.handle(conn, msg_json)
                else:
                    conn.logger.bind(tag=TAG).error(f"Received message of unknown type: {message}")
            # Echo numeric messages.
            elif isinstance(msg_json, int):
                conn.logger.bind(tag=TAG).info(f"Received numeric message: {message}")
                await conn.websocket.send(message)

        except json.JSONDecodeError:
            # Preserve the existing fallback for non-JSON messages.
            conn.logger.bind(tag=TAG).error(f"Failed to parse message: {message}")
            await conn.websocket.send(message)
