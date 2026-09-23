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
from plugins_func.functions.web_search import _search_metaso, _search_tavily


description = "Benchmark the configured web search provider"


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


def configured_search(config):
    search_config = config.get("plugins", {}).get("web_search", {})
    provider = str(search_config.get("provider", "")).strip().lower()
    if provider not in {"metaso", "tavily"}:
        raise ValueError("plugins.web_search.provider must be metaso or tavily")

    api_key = str(search_config.get("api_key", "")).strip()
    if not api_key or any(
        marker in api_key.lower()
        for marker in ("your_", "your ", "placeholder", "xxx", "你的")
    ):
        raise ValueError("plugins.web_search.api_key is not configured")
    return provider, api_key, search_config


def select_queries(config, count, seed):
    override = os.getenv("PERF_WEB_SEARCH_QUERY")
    queries = [override] if override else config.get("module_test", {}).get(
        "web_search_queries", []
    )
    queries = [str(query).strip() for query in queries if str(query).strip()]
    if not queries:
        queries = ["What are today's major technology news stories?"]

    rng = random.Random(seed)
    selected = []
    while len(selected) < count:
        batch = queries.copy()
        rng.shuffle(batch)
        selected.extend(batch)
    return queries, selected[:count]


async def execute_search(provider, api_key, search_config, query):
    max_results = int(search_config.get("max_results", 5))
    if provider == "metaso":
        return await _search_metaso(api_key, query, max_results)
    return await _search_tavily(
        api_key,
        query,
        max_results,
        search_depth=str(search_config.get("search_depth", "advanced")),
        include_answer=search_config.get("include_answer", "advanced"),
        country=str(search_config.get("country", "") or ""),
        language=str(search_config.get("language", "") or ""),
    )


async def main():
    config = await load_config()
    provider, api_key, search_config = configured_search(config)
    runs = positive_int_from_env("PERF_RUNS", 3)
    timeout = positive_int_from_env("PERF_TIMEOUT_SECONDS", 30)
    preview_chars = non_negative_int_from_env(
        "PERF_WEB_SEARCH_PREVIEW_CHARS", 500
    )
    seed = int(os.getenv("PERF_WEB_SEARCH_SEED", 42))
    query_set, queries = select_queries(config, runs, seed)
    initial_usage = process_usage()
    durations = []

    print(f"Web search provider: {provider}")
    print(f"Samples: {runs}; timeout per sample: {timeout}s")
    print(f"Query set: {len(query_set)}; seed: {seed}")
    print(
        "Result preview: "
        f"{'full' if preview_chars == 0 else f'{preview_chars} characters'}"
    )
    if provider == "tavily":
        print(
            "Tavily options: "
            f"depth={search_config.get('search_depth', 'advanced')}, "
            f"country={search_config.get('country') or 'auto'}, "
            f"language={search_config.get('language') or 'auto'}"
        )

    for sample_number, query in enumerate(queries, 1):
        started_at = time.perf_counter()
        try:
            result = await asyncio.wait_for(
                execute_search(provider, api_key, search_config, query),
                timeout=timeout,
            )
            duration = time.perf_counter() - started_at
            result = str(result or "").strip()
            if not result or result == "No relevant search results were found.":
                raise RuntimeError("Provider returned no search results")
            durations.append(duration)
            print(f"\nSample {sample_number}/{runs}: {query}")
            preview = result if preview_chars == 0 else result[:preview_chars]
            suffix = "" if len(preview) == len(result) else "\n...[truncated]"
            print(f"Search {duration:.3f}s - {preview}{suffix}")
        except asyncio.TimeoutError:
            print(f"\nSample {sample_number}/{runs}: timed out after {timeout}s")
        except Exception as error:
            print(
                f"\nSample {sample_number}/{runs}: failed - "
                f"{type(error).__name__}: {error}"
            )

    print("\nWeb search benchmark summary")
    print(f"Success rate: {len(durations)}/{runs} ({len(durations) / runs:.0%})")
    if durations:
        print(format_stats("Search latency", durations))
    print_benchmark_usage(initial_usage)


if __name__ == "__main__":
    asyncio.run(main())
