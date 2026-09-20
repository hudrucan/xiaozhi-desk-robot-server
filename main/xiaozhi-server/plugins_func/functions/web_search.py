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

WEB_SEARCH_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": _DEFAULT_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query or question.",
                }
            },
            "required": ["query"],
        },
    },
}


async def _search_metaso(api_key: str, query: str, max_results: int) -> str:
    """Call the Metaso search API."""
    url = "https://metaso.cn/api/v1/search"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "q": query,
        "size": max_results,
        "stream": False,
        "scope": "webpage",
        "includeSummary": True,
        "includeRawContent": False,
        "conciseSnippet": False,
    }
    logger.bind(tag=TAG).debug(f"Metaso request | URL: {url} | payload: {payload}")
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=3.0)) as client:
        response = await client.post(url, json=payload, headers=headers)
    data = response.json()
    logger.bind(tag=TAG).debug(f"Metaso response | status: {response.status_code}")

    webpages = data.get("webpages", [])
    if not webpages:
        return "No relevant search results were found."

    lines = ["【联网搜索结果】"]
    for i, item in enumerate(webpages, 1):
        title = item.get("title", "无标题")
        snippet = item.get("summary", "")
        date = item.get("date", "")
        lines.append(f"{i}. 标题：{title}")
        if date:
            lines.append(f"   日期：{date}")
        if snippet:
            lines.append(f"   摘要：{snippet}")

    return "\n".join(lines)


async def _search_tavily(api_key: str, query: str, max_results: int) -> str:
    """Call the Tavily search API."""
    url = "https://api.tavily.com/search"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "query": query,
        "max_results": max_results,
        "search_depth": "advanced",
        "include_answer": "advanced",
    }
    logger.bind(tag=TAG).debug(f"Tavily request | URL: {url} | payload: {payload}")
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=3.0)) as client:
        response = await client.post(url, json=payload, headers=headers)
    data = response.json()
    logger.bind(tag=TAG).debug(f"Tavily response | status: {response.status_code} | data: {data}")

    results = data.get("results", [])
    if not results:
        return "No relevant search results were found."

    answer = data.get("answer", "")
    lines = [f"【联网搜索结果】\n总结：{answer}"]
    # for i, item in enumerate(results, 1):
    #     title = item.get("title", "无标题")
    #     summary = item.get("content", "")
    #     lines.append(f"{i}. 标题：{title}")
    #     if summary:
    #         lines.append(f"   摘要：{summary}")

    return "\n".join(lines)


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
            result_text = await _search_tavily(api_key, query, max_results)
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
