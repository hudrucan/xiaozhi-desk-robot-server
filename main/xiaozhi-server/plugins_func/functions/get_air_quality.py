from typing import TYPE_CHECKING

from config.logger import setup_logging
from plugins_func.functions.air_quality.open_meteo import fetch_air_quality_report
from plugins_func.register import Action, ActionResponse, ToolType, register_function

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

GET_AIR_QUALITY_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "get_air_quality",
        "description": (
            "Get current air quality, particle pollution, UV index, and a short "
            "forecast summary for a location. Call this on every request for "
            "current air quality, even if an earlier turn contains a previous "
            "result."
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
                "forecast_hours": {
                    "type": "integer",
                    "description": "Optional forecast window from 1 to 168 hours.",
                    "minimum": 1,
                    "maximum": 168,
                },
            },
            "required": [],
        },
    },
}


@register_function(
    "get_air_quality",
    GET_AIR_QUALITY_FUNCTION_DESC,
    ToolType.SYSTEM_CTL,
)
async def get_air_quality(
    conn: "ConnectionHandler",
    location: str = None,
    lang: str = None,
    forecast_hours: int = None,
):
    from core.utils.cache.manager import CacheType, cache_manager

    air_quality_config = conn.config.get("plugins", {}).get("get_air_quality", {})
    provider = str(air_quality_config.get("provider", "")).strip().lower()
    if provider != "open_meteo":
        return ActionResponse(
            Action.ERROR,
            "Air-quality tool is not configured",
            None,
        )

    location = str(
        location or air_quality_config.get("default_location", "")
    ).strip()
    if not location:
        return ActionResponse(
            Action.REQLLM,
            "No air-quality location was provided",
            None,
        )

    language = str(
        lang or air_quality_config.get("language", "en")
    ).strip() or "en"
    preferred_country_code = str(
        air_quality_config.get("preferred_country_code", "")
    ).strip()
    if forecast_hours is None:
        forecast_hours = air_quality_config.get("forecast_hours", 24)
    forecast_hours = max(1, min(int(forecast_hours), 168))
    cache_ttl_seconds = max(
        60,
        int(air_quality_config.get("cache_ttl_seconds", 1800)),
    )

    cache_key = (
        f"air_quality:open_meteo:{location.casefold()}:{language.casefold()}:"
        f"{preferred_country_code.upper()}:{forecast_hours}"
    )
    cached_report = cache_manager.get(CacheType.AIR_QUALITY, cache_key)
    if cached_report:
        return ActionResponse(Action.REQLLM, cached_report, None)

    try:
        report = await fetch_air_quality_report(
            location=location,
            language=language,
            preferred_country_code=preferred_country_code,
            forecast_hours=forecast_hours,
        )
    except Exception as error:
        logger.bind(tag=TAG).error(f"Air-quality request failed: {error}")
        return ActionResponse(
            Action.REQLLM,
            "Air-quality request failed",
            None,
        )

    if not report:
        return ActionResponse(
            Action.REQLLM,
            f"No matching air-quality location was found for: {location}",
            None,
        )

    cache_manager.set(
        CacheType.AIR_QUALITY,
        cache_key,
        report,
        ttl=cache_ttl_seconds,
    )
    return ActionResponse(Action.REQLLM, report, None)
