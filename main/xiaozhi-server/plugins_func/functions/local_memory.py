import asyncio
from typing import TYPE_CHECKING

from core.memory_storage import MemoryStorageError

from plugins_func.register import Action, ActionResponse, ToolType, register_function
from plugins_func.tool_schemas import MANAGE_MEMORY_FUNCTION_DESC

if TYPE_CHECKING:
    from core.connection import ConnectionHandler


def _response(conn: "ConnectionHandler", key: str, default: str):
    selected = conn.config.get("selected_module", {}).get("Memory", "mem_local_explicit")
    responses = (
        conn.config.get("Memory", {})
        .get(selected, {})
        .get("responses", {})
    )
    return str(responses.get(key, default))


def _explicit_memory(conn: "ConnectionHandler"):
    selected_memory = conn.config.get("selected_module", {}).get("Memory")
    provider_config = conn.config.get("Memory", {}).get(selected_memory, {})
    if provider_config.get("type", selected_memory) != "mem_local_explicit":
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
    conn: "ConnectionHandler",
    action: str,
    content: str = None,
    type: str = None,
    project: str = None,
    entities: list = None,
    tags: list = None,
    importance: int = None,
    pinned: bool = None,
    active: bool = None,
    supersedes: str = None,
):
    memory = _explicit_memory(conn)
    if memory is None:
        return ActionResponse(
            Action.ERROR,
            response=_response(conn, "unavailable", "Local memory is not enabled."),
        )

    normalized_action = str(action or "").strip().lower()
    if normalized_action == "remember":
        metadata = {
            key: value
            for key, value in {
                "type": type,
                "project": project,
                "entities": entities,
                "tags": tags,
                "importance": importance,
                "pinned": pinned,
                "active": active,
                "supersedes": supersedes,
            }.items()
            if value is not None
        }
        try:
            remembered = await asyncio.to_thread(memory.remember, content, **metadata)
        except MemoryStorageError as error:
            return ActionResponse(Action.ERROR, response=str(error))
        except OSError:
            return ActionResponse(Action.ERROR, response="Memory storage unavailable")
        except ValueError as error:
            return ActionResponse(Action.ERROR, response=str(error))
        if not remembered:
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
        recalled = memory.recall(
            content,
            context={
                "active_project": getattr(conn, "active_memory_project", None),
            },
        )
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
                "Answer from these facts. Call memory again only if the user "
                "explicitly asked to save, replace, or forget information."
            ),
        )
    elif normalized_action == "forget":
        try:
            removed = await asyncio.to_thread(memory.forget, content)
        except MemoryStorageError as error:
            return ActionResponse(Action.ERROR, response=str(error))
        except OSError:
            return ActionResponse(Action.ERROR, response="Memory storage unavailable")
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
