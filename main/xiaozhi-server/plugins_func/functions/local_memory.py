from typing import TYPE_CHECKING

from plugins_func.register import Action, ActionResponse, ToolType, register_function

if TYPE_CHECKING:
    from core.connection import ConnectionHandler


MANAGE_MEMORY_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "manage_memory",
        "description": (
            "Manage durable local memory only when the user explicitly asks to "
            "remember or forget information, asks what is saved, or asks a "
            "question that depends on a previously saved personal fact. Use "
            "recall before answering questions about saved facts. Never save "
            "ordinary conversation automatically."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["remember", "recall", "forget", "list"],
                },
                "content": {
                    "type": "string",
                    "description": (
                        "A concise fact to save, or a short search phrase for "
                        "recall or deletion. Omit only for list."
                    ),
                }
            },
            "required": ["action"],
        },
    },
}


def _response(conn: "ConnectionHandler", key: str, default: str):
    responses = (
        conn.config.get("Memory", {})
        .get("mem_local_explicit", {})
        .get("responses", {})
    )
    return str(responses.get(key, default))


def _explicit_memory(conn: "ConnectionHandler"):
    selected_memory = conn.config.get("selected_module", {}).get("Memory")
    if selected_memory != "mem_local_explicit":
        return None
    memory = getattr(conn, "memory", None)
    if not all(
        callable(getattr(memory, method, None))
        for method in ("remember", "recall", "forget", "list_entries")
    ):
        return None
    return memory


@register_function("manage_memory", MANAGE_MEMORY_FUNCTION_DESC, ToolType.SYSTEM_CTL)
async def manage_memory(
    conn: "ConnectionHandler", action: str, content: str = None
):
    memory = _explicit_memory(conn)
    if memory is None:
        return ActionResponse(
            Action.ERROR,
            response=_response(conn, "unavailable", "Local memory is not enabled."),
        )

    normalized_action = str(action or "").strip().lower()
    if normalized_action == "remember":
        if not memory.remember(content):
            return ActionResponse(
                Action.ERROR,
                response=_response(
                    conn, "missing_content", "No memory content was provided."
                ),
            )
        response = _response(conn, "remembered", "I will remember that.")
    elif normalized_action == "recall":
        if not memory.recall_enabled:
            return ActionResponse(
                Action.ERROR,
                response=_response(
                    conn, "recall_disabled", "Memory recall is disabled."
                ),
            )
        if not str(content or "").strip():
            return ActionResponse(
                Action.ERROR,
                response=_response(
                    conn, "missing_content", "No memory content was provided."
                ),
            )
        recalled = memory.recall(content)
        if not recalled:
            return ActionResponse(
                Action.RESPONSE,
                response=_response(
                    conn, "not_found", "I could not find a matching memory."
                ),
            )
        return ActionResponse(
            Action.REQLLM,
            result=(
                "Saved local memories relevant to the user's request:\n"
                f"{recalled}\n"
                "Answer from these facts and do not call the memory tool again."
            ),
        )
    elif normalized_action == "forget":
        removed = memory.forget(content)
        if removed:
            response = _response(conn, "forgotten", "I forgot that.")
        else:
            response = _response(
                conn, "not_found", "I could not find a matching memory."
            )
    elif normalized_action == "list":
        entries = memory.list_entries()
        response = (
            "\n".join(f"- {entry}" for entry in entries)
            if entries
            else _response(conn, "empty", "I do not have any saved memories yet.")
        )
    else:
        return ActionResponse(
            Action.ERROR,
            response=_response(conn, "unsupported_action", "Unsupported memory action."),
        )

    return ActionResponse(Action.RESPONSE, response=response)
