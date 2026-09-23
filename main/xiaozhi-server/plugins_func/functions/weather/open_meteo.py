import httpx


GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

WMO_WEATHER_CODES = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "rime fog",
    51: "light drizzle",
    53: "moderate drizzle",
    55: "dense drizzle",
    56: "light freezing drizzle",
    57: "dense freezing drizzle",
    61: "slight rain",
    63: "moderate rain",
    65: "heavy rain",
    66: "light freezing rain",
    67: "heavy freezing rain",
    71: "slight snowfall",
    73: "moderate snowfall",
    75: "heavy snowfall",
    77: "snow grains",
    80: "slight rain showers",
    81: "moderate rain showers",
    82: "violent rain showers",
    85: "slight snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with slight hail",
    99: "thunderstorm with heavy hail",
}


def normalize_language(language):
    normalized = str(language or "en").strip().lower().replace("_", "-")
    return normalized.split("-", 1)[0] or "en"


def describe_weather(code):
    try:
        return WMO_WEATHER_CODES.get(int(code), "unknown conditions")
    except (TypeError, ValueError):
        return "unknown conditions"


def format_number(value, suffix=""):
    if value is None:
        return "unknown"
    if isinstance(value, float):
        value = round(value, 1)
    return f"{value}{suffix}"


def value_at(values, index):
    return values[index] if isinstance(values, list) and index < len(values) else None


def location_label(location):
    parts = []
    for key in ("name", "admin1", "country"):
        value = str(location.get(key, "")).strip()
        if value and value.casefold() not in {part.casefold() for part in parts}:
            parts.append(value)
    return ", ".join(parts)


async def lookup_location(client, name, language, preferred_country_code):
    response = await client.get(
        GEOCODING_URL,
        params={
            "name": name,
            "count": 10,
            "language": normalize_language(language),
            "format": "json",
        },
    )
    response.raise_for_status()
    results = response.json().get("results", [])
    if not results:
        return None

    preferred = str(preferred_country_code or "").strip().upper()
    if preferred:
        for result in results:
            if str(result.get("country_code", "")).upper() == preferred:
                return result
    return results[0]


async def fetch_forecast(client, location, forecast_days):
    response = await client.get(
        FORECAST_URL,
        params={
            "latitude": location["latitude"],
            "longitude": location["longitude"],
            "current": (
                "temperature_2m,apparent_temperature,relative_humidity_2m,"
                "precipitation,weather_code,wind_speed_10m"
            ),
            "daily": (
                "weather_code,temperature_2m_max,temperature_2m_min,"
                "precipitation_probability_max,precipitation_sum"
            ),
            "timezone": "auto",
            "forecast_days": forecast_days,
        },
    )
    response.raise_for_status()
    return response.json()


def build_weather_report(location, forecast):
    current = forecast.get("current", {})
    current_units = forecast.get("current_units", {})
    daily = forecast.get("daily", {})
    daily_units = forecast.get("daily_units", {})

    lines = [
        f"Location: {location_label(location)}",
        f"Timezone: {forecast.get('timezone', location.get('timezone', 'unknown'))}",
        f"Observation time: {current.get('time', 'unknown')}",
        f"Current conditions: {describe_weather(current.get('weather_code'))}",
        (
            "Temperature: "
            f"{format_number(current.get('temperature_2m'), current_units.get('temperature_2m', ''))}; "
            "feels like "
            f"{format_number(current.get('apparent_temperature'), current_units.get('apparent_temperature', ''))}"
        ),
        (
            "Humidity: "
            f"{format_number(current.get('relative_humidity_2m'), current_units.get('relative_humidity_2m', ''))}; "
            "wind: "
            f"{format_number(current.get('wind_speed_10m'), current_units.get('wind_speed_10m', ''))}; "
            "precipitation: "
            f"{format_number(current.get('precipitation'), current_units.get('precipitation', ''))}"
        ),
        "Forecast:",
    ]

    dates = daily.get("time", [])
    for index, date in enumerate(dates):
        condition = describe_weather(value_at(daily.get("weather_code"), index))
        low = format_number(
            value_at(daily.get("temperature_2m_min"), index),
            daily_units.get("temperature_2m_min", ""),
        )
        high = format_number(
            value_at(daily.get("temperature_2m_max"), index),
            daily_units.get("temperature_2m_max", ""),
        )
        rain_chance = format_number(
            value_at(daily.get("precipitation_probability_max"), index),
            daily_units.get("precipitation_probability_max", ""),
        )
        precipitation = format_number(
            value_at(daily.get("precipitation_sum"), index),
            daily_units.get("precipitation_sum", ""),
        )
        lines.append(
            f"- {date}: {condition}; {low} to {high}; "
            f"rain chance {rain_chance}; precipitation {precipitation}"
        )

    lines.append("Source: Open-Meteo")
    return "\n".join(lines)


async def fetch_weather_report(
    location, language="en", preferred_country_code="", forecast_days=7
):
    timeout = httpx.Timeout(8.0, connect=3.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resolved_location = await lookup_location(
            client,
            location,
            language,
            preferred_country_code,
        )
        if not resolved_location:
            return None
        forecast = await fetch_forecast(client, resolved_location, forecast_days)
    return build_weather_report(resolved_location, forecast)
