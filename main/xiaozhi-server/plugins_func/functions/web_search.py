from plugins_func.functions.search.http_search import _search_metaso, _search_tavily
import httpx
from config.logger import setup_logging
from plugins_func.register import (
    register_function,
    ToolType,
    ActionResponse,
    Action,
)
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

_DEFAULT_DESCRIPTION = (
    "Search the web when the user explicitly needs current online information."
)

from plugins_func.tool_schemas import WEB_SEARCH_FUNCTION_DESC






@register_function("web_search", WEB_SEARCH_FUNCTION_DESC, ToolType.SYSTEM_CTL)
async def web_search(conn: "ConnectionHandler", query: str = None):
    logger.bind(tag=TAG).info(f"web_search called | query={query}")
    if not query:
        return ActionResponse(Action.REQLLM, "A search query is required.", None)

    web_search_config = conn.config.get("plugins", {}).get("web_search", {})
    provider = web_search_config.get("provider", "").lower()
    max_results = int(web_search_config.get("max_results", 3))
    logger.bind(tag=TAG).debug(
        f"web_search config | provider={provider} | max_results={max_results}"
    )

    api_key = web_search_config.get("api_key", "")
    if not api_key:
        return ActionResponse(
            Action.REQLLM,
            "Web search is not configured.",
            None,
        )

    try:
        if provider == "metaso":
            result_text = await _search_metaso(api_key, query, max_results)
        elif provider == "tavily":
            result_text = await _search_tavily(
                api_key,
                query,
                max_results,
                search_depth=str(web_search_config.get("search_depth", "advanced")),
                include_answer=web_search_config.get("include_answer", "advanced"),
                country=str(web_search_config.get("country", "") or ""),
                language=str(web_search_config.get("language", "") or ""),
            )
        else:
            return ActionResponse(
                Action.REQLLM,
                f"Unsupported web search provider: {provider}",
                None,
            )
        logger.bind(tag=TAG).debug("Web search result assembled")
    except httpx.TimeoutException:
        logger.bind(tag=TAG).error("Web search request timed out")
        result_text = "The web search request timed out."
    except httpx.HTTPStatusError as e:
        logger.bind(tag=TAG).error(f"Web search request failed: {e}")
        result_text = "The web search request failed."
    except Exception as e:
        logger.bind(tag=TAG).error(f"Unexpected web search error: {e}")
        result_text = "The web search request failed."

    return ActionResponse(Action.REQLLM, result_text, None)
