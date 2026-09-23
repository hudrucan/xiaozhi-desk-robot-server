from typing import TYPE_CHECKING

from config.logger import setup_logging
from plugins_func.functions.weather.open_meteo import fetch_weather_report
from plugins_func.register import Action, ActionResponse, ToolType, register_function

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

GET_WEATHER_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": (
            "Get current conditions and a multi-day forecast for a location. "
            "Local weather may already be present in context; call this tool "
            "when it is missing or the user asks about another location."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "Optional city or location name.",
                },
                "lang": {
                    "type": "string",
                    "description": "Optional ISO 639-1 language code for place names.",
                },
            },
            "required": [],
        },
    },
}


@register_function("get_weather", GET_WEATHER_FUNCTION_DESC, ToolType.SYSTEM_CTL)
async def get_weather(
    conn: "ConnectionHandler", location: str = None, lang: str = None
):
    from core.utils.cache.manager import CacheType, cache_manager

    weather_config = conn.config.get("plugins", {}).get("get_weather", {})
    provider = str(weather_config.get("provider", "")).strip().lower()
    if provider != "open_meteo":
        return ActionResponse(Action.ERROR, "Weather tool is not configured", None)

    location = str(location or weather_config.get("default_location", "")).strip()
    if not location:
        return ActionResponse(Action.REQLLM, "No weather location was provided", None)

    language = str(lang or weather_config.get("language", "en")).strip() or "en"
    preferred_country_code = str(
        weather_config.get("preferred_country_code", "")
    ).strip()
    forecast_days = int(weather_config.get("forecast_days", 7))
    forecast_days = max(1, min(forecast_days, 16))
    cache_ttl_seconds = max(
        60, int(weather_config.get("cache_ttl_seconds", 1800))
    )

    cache_key = (
        f"weather:open_meteo:{location.casefold()}:{language.casefold()}:"
        f"{preferred_country_code.upper()}:{forecast_days}"
    )
    cached_report = cache_manager.get(CacheType.WEATHER, cache_key)
    if cached_report:
        return ActionResponse(Action.REQLLM, cached_report, None)

    try:
        weather_report = await fetch_weather_report(
            location=location,
            language=language,
            preferred_country_code=preferred_country_code,
            forecast_days=forecast_days,
        )
    except Exception as error:
        logger.bind(tag=TAG).error(f"Weather request failed: {error}")
        return ActionResponse(Action.REQLLM, "Weather request failed", None)

    if not weather_report:
        return ActionResponse(
            Action.REQLLM,
            f"No matching weather location was found for: {location}",
            None,
        )

    cache_manager.set(
        CacheType.WEATHER,
        cache_key,
        weather_report,
        ttl=cache_ttl_seconds,
    )
    return ActionResponse(Action.REQLLM, weather_report, None)
