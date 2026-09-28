from typing import Any, Dict

from core.handle.textMessageHandler import TextMessageHandler
from core.handle.textMessageType import TextMessageType


class GoodbyeMessageHandler(TextMessageHandler):
    """End a logical conversation without necessarily closing its transport."""

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.GOODBYE

    async def handle(self, conn, msg_json: Dict[str, Any]) -> None:
        await conn.end_conversation("client_goodbye", notify_client=False)
