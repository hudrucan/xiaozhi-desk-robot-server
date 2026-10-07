import json
from datetime import datetime
from typing import TYPE_CHECKING

from plugins_func.register import Action, ActionResponse, ToolType, register_function

if TYPE_CHECKING:
    from core.connection import ConnectionHandler


from plugins_func.tool_schemas import GET_CURRENT_DATETIME_FUNCTION_DESC


@register_function(
    "get_current_datetime",
    GET_CURRENT_DATETIME_FUNCTION_DESC,
    ToolType.SYSTEM_CTL,
)
async def get_current_datetime(conn: "ConnectionHandler"):
    now = datetime.now().astimezone()
    result = {
        "observed_at": now.isoformat(timespec="seconds"),
        "date": now.date().isoformat(),
        "weekday": now.strftime("%A"),
        "iso_weekday": now.isoweekday(),
        "time": now.strftime("%H:%M:%S"),
        "utc_offset": now.strftime("%z"),
        "timezone": str(now.tzinfo),
    }
    return ActionResponse(Action.REQLLM, json.dumps(result), None)
