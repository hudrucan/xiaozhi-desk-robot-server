import httpx

from plugins_func.functions.weather.open_meteo import (
    format_number,
    location_label,
    lookup_location,
)


AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

CURRENT_VARIABLES = (
    "us_aqi,pm2_5,pm10,carbon_monoxide,nitrogen_dioxide,"
    "sulphur_dioxide,ozone,uv_index"
)
HOURLY_VARIABLES = "us_aqi,pm2_5,pm10,uv_index"


def describe_us_aqi(value):
    try:
        aqi = float(value)
    except (TypeError, ValueError):
        return "unknown"

    if aqi <= 50:
        return "good"
    if aqi <= 100:
        return "moderate"
    if aqi <= 150:
        return "unhealthy for sensitive groups"
    if aqi <= 200:
        return "unhealthy"
    if aqi <= 300:
        return "very unhealthy"
    return "hazardous"


def describe_uv_index(value):
    try:
        uv_index = float(value)
    except (TypeError, ValueError):
        return "unknown"

    if uv_index < 3:
        return "low"
    if uv_index < 6:
        return "moderate"
    if uv_index < 8:
        return "high"
    if uv_index < 11:
        return "very high"
    return "extreme"


def outdoor_activity_note(aqi):
    try:
        value = float(aqi)
    except (TypeError, ValueError):
        return "No activity note is available because the AQI is unknown."

    if value <= 50:
        return "Air quality is generally suitable for normal outdoor activity."
    if value <= 100:
        return (
            "Unusually sensitive people may prefer shorter intense outdoor "
            "activity."
        )
    if value <= 150:
        return (
            "Sensitive groups should consider reducing prolonged or intense "
            "outdoor activity."
        )
    if value <= 200:
        return "Consider reducing prolonged or intense outdoor activity."
    return "Avoid prolonged or intense outdoor activity when practical."


def peak_with_time(values, times):
    if not isinstance(values, list):
        return None, None

    candidates = []
    for index, value in enumerate(values):
        if value is None:
            continue
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            continue
        time = (
            times[index]
            if isinstance(times, list) and index < len(times)
            else None
        )
        candidates.append((numeric_value, time))

    return max(candidates, default=(None, None), key=lambda item: item[0])


async def fetch_air_quality(client, location, forecast_hours):
    response = await client.get(
        AIR_QUALITY_URL,
        params={
            "latitude": location["latitude"],
            "longitude": location["longitude"],
            "current": CURRENT_VARIABLES,
            "hourly": HOURLY_VARIABLES,
            "timezone": "auto",
            "forecast_hours": forecast_hours,
        },
    )
    response.raise_for_status()
    return response.json()


def build_air_quality_report(location, air_quality, forecast_hours):
    current = air_quality.get("current", {})
    current_units = air_quality.get("current_units", {})
    hourly = air_quality.get("hourly", {})
    hourly_units = air_quality.get("hourly_units", {})
    times = hourly.get("time", [])

    current_aqi = current.get("us_aqi")
    current_uv = current.get("uv_index")
    peak_aqi, peak_aqi_time = peak_with_time(hourly.get("us_aqi"), times)
    peak_pm2_5, peak_pm2_5_time = peak_with_time(hourly.get("pm2_5"), times)
    peak_uv, peak_uv_time = peak_with_time(hourly.get("uv_index"), times)
    ozone = format_number(current.get("ozone"), current_units.get("ozone", ""))
    nitrogen_dioxide = format_number(
        current.get("nitrogen_dioxide"),
        current_units.get("nitrogen_dioxide", ""),
    )
    sulphur_dioxide = format_number(
        current.get("sulphur_dioxide"),
        current_units.get("sulphur_dioxide", ""),
    )
    carbon_monoxide = format_number(
        current.get("carbon_monoxide"),
        current_units.get("carbon_monoxide", ""),
    )

    lines = [
        f"Location: {location_label(location)}",
        f"Timezone: {air_quality.get('timezone', location.get('timezone', 'unknown'))}",
        f"Observation time: {current.get('time', 'unknown')}",
        (
            "US AQI: "
            f"{format_number(current_aqi)} ({describe_us_aqi(current_aqi)})"
        ),
        (
            "Particles: PM2.5 "
            f"{format_number(current.get('pm2_5'), current_units.get('pm2_5', ''))}; "
            "PM10 "
            f"{format_number(current.get('pm10'), current_units.get('pm10', ''))}"
        ),
        (
            f"Gases: ozone {ozone}; NO2 {nitrogen_dioxide}; "
            f"SO2 {sulphur_dioxide}; CO {carbon_monoxide}"
        ),
        (
            "UV index: "
            f"{format_number(current_uv)} ({describe_uv_index(current_uv)})"
        ),
        f"Forecast summary for the next {forecast_hours} hours:",
        (
            "- Peak US AQI: "
            f"{format_number(peak_aqi)} ({describe_us_aqi(peak_aqi)})"
            f" at {peak_aqi_time or 'unknown'}"
        ),
        (
            "- Peak PM2.5: "
            f"{format_number(peak_pm2_5, hourly_units.get('pm2_5', ''))}"
            f" at {peak_pm2_5_time or 'unknown'}"
        ),
        (
            "- Peak UV index: "
            f"{format_number(peak_uv)} ({describe_uv_index(peak_uv)})"
            f" at {peak_uv_time or 'unknown'}"
        ),
        f"Current outdoor activity note: {outdoor_activity_note(current_aqi)}",
        "Source: Open-Meteo Air Quality API",
    ]
    return "\n".join(lines)


async def fetch_air_quality_report(
    location,
    language="en",
    preferred_country_code="",
    forecast_hours=24,
):
    forecast_hours = max(1, min(int(forecast_hours), 168))
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
        air_quality = await fetch_air_quality(
            client,
            resolved_location,
            forecast_hours,
        )
    return build_air_quality_report(
        resolved_location,
        air_quality,
        forecast_hours,
    )
