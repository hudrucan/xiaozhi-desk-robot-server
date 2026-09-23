import asyncio
import math
import os
import random
import statistics
import time

from config.settings import load_config
from performance_testers.resource_usage import (
    print_benchmark_usage,
    process_usage,
)
from plugins_func.functions.weather.open_meteo import fetch_weather_report


description = "Benchmark the configured weather plugin"


def positive_int_from_env(name, default):
    value = int(os.getenv(name, default))
    if value < 1:
        raise ValueError(f"{name} must be at least 1")
    return value


def non_negative_int_from_env(name, default):
    value = int(os.getenv(name, default))
    if value < 0:
        raise ValueError(f"{name} must be zero or greater")
    return value


def format_stats(label, values):
    ordered = sorted(values)
    p95_index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return (
        f"{label}: min {min(values):.3f}s, "
        f"median {statistics.median(values):.3f}s, "
        f"mean {statistics.mean(values):.3f}s, "
        f"p95 {ordered[p95_index]:.3f}s, max {max(values):.3f}s"
    )


def configured_weather(config):
    weather_config = config.get("plugins", {}).get("get_weather", {})
    provider = str(weather_config.get("provider", "")).strip().lower()
    if provider != "open_meteo":
        raise ValueError("plugins.get_weather.provider must be open_meteo")
    return provider, weather_config


def select_locations(config, weather_config, count, seed):
    override = os.getenv("PERF_WEATHER_LOCATION")
    if override:
        locations = [override]
    else:
        locations = config.get("module_test", {}).get("weather_locations", [])
        locations = [
            str(location).strip() for location in locations if str(location).strip()
        ]
        default_location = str(
            weather_config.get("default_location", "")
        ).strip()
        if default_location:
            locations = [default_location] + [
                location
                for location in locations
                if location.casefold() != default_location.casefold()
            ]

    if not locations:
        raise ValueError(
            "Set PERF_WEATHER_LOCATION or plugins.get_weather.default_location"
        )

    rng = random.Random(seed)
    selected = []
    while len(selected) < count:
        batch = locations.copy()
        rng.shuffle(batch)
        selected.extend(batch)
    return locations, selected[:count]


async def main():
    config = await load_config()
    provider, weather_config = configured_weather(config)
    runs = positive_int_from_env("PERF_RUNS", 3)
    timeout = positive_int_from_env("PERF_TIMEOUT_SECONDS", 30)
    preview_chars = non_negative_int_from_env("PERF_WEATHER_PREVIEW_CHARS", 500)
    seed = int(os.getenv("PERF_WEATHER_SEED", 42))
    locations, selected_locations = select_locations(
        config, weather_config, runs, seed
    )
    language = str(weather_config.get("language", "en")).strip() or "en"
    preferred_country_code = str(
        weather_config.get("preferred_country_code", "")
    ).strip()
    forecast_days = max(
        1, min(int(weather_config.get("forecast_days", 7)), 16)
    )
    initial_usage = process_usage()
    durations = []

    print(f"Weather provider: {provider}")
    print(f"Samples: {runs}; timeout per sample: {timeout}s")
    print(f"Location set: {len(locations)}; seed: {seed}")
    print(
        f"Options: language={language}, "
        f"preferred_country={preferred_country_code or 'none'}, "
        f"forecast_days={forecast_days}"
    )
    print(
        "Result preview: "
        f"{'full' if preview_chars == 0 else f'{preview_chars} characters'}"
    )

    for sample_number, location in enumerate(selected_locations, 1):
        started_at = time.perf_counter()
        try:
            report = await asyncio.wait_for(
                fetch_weather_report(
                    location=location,
                    language=language,
                    preferred_country_code=preferred_country_code,
                    forecast_days=forecast_days,
                ),
                timeout=timeout,
            )
            duration = time.perf_counter() - started_at
            report = str(report or "").strip()
            if not report:
                raise RuntimeError("Provider returned no matching weather location")

            durations.append(duration)
            preview = report if preview_chars == 0 else report[:preview_chars]
            suffix = "" if len(preview) == len(report) else "\n...[truncated]"
            print(f"\nSample {sample_number}/{runs}: {location}")
            print(f"Weather {duration:.3f}s - {preview}{suffix}")
        except asyncio.TimeoutError:
            print(f"\nSample {sample_number}/{runs}: timed out after {timeout}s")
        except Exception as error:
            print(
                f"\nSample {sample_number}/{runs}: failed - "
                f"{type(error).__name__}: {error}"
            )

    print("\nWeather benchmark summary")
    print(f"Success rate: {len(durations)}/{runs} ({len(durations) / runs:.0%})")
    if durations:
        print(format_stats("Weather latency", durations))
    print_benchmark_usage(initial_usage)


if __name__ == "__main__":
    asyncio.run(main())
