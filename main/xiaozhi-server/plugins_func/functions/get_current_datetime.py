import json
from datetime import datetime
from typing import TYPE_CHECKING

from plugins_func.register import Action, ActionResponse, ToolType, register_function

if TYPE_CHECKING:
    from core.connection import ConnectionHandler


GET_CURRENT_DATETIME_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "get_current_datetime",
        "description": (
            "Get the server's authoritative current local date, weekday, time, "
            "and UTC offset. Call this whenever the user asks what date, day of "
            "the week, or time it is now."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}


@register_function(
    "get_current_datetime",
    GET_CURRENT_DATETIME_FUNCTION_DESC,
    ToolType.SYSTEM_CTL,
)
async def get_current_datetime(conn: "ConnectionHandler"):
    now = datetime.now().astimezone()
    result = {
        "date": now.date().isoformat(),
        "weekday": now.strftime("%A"),
        "time": now.strftime("%H:%M:%S"),
        "utc_offset": now.strftime("%z"),
        "timezone": str(now.tzinfo),
    }
    return ActionResponse(Action.REQLLM, json.dumps(result), None)
