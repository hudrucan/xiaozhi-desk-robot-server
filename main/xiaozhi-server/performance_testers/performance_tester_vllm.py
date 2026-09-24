import asyncio
import base64
import logging
import math
import os
import statistics
import time
from pathlib import Path

from config.settings import load_config
from core.utils.vllm import (
    create_instance as create_vllm_instance,
    resolve_provider_config,
)
from performance_testers.resource_usage import (
    print_benchmark_usage,
    print_initialization_usage,
    process_usage,
)


logging.basicConfig(level=logging.WARNING)

description = "Benchmark the selected VLLM provider"


def positive_int_from_env(name, default):
    value = int(os.getenv(name, default))
    if value < 1:
        raise ValueError(f"{name} must be at least 1")
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


def selected_provider(config):
    provider_name = config.get("selected_module", {}).get("VLLM")
    if not provider_name:
        raise ValueError("No VLLM provider is selected")

    if config.get("VLLM", {}).get(provider_name) is None:
        raise ValueError(f"Missing configuration for VLLM provider: {provider_name}")
    provider_type, provider_config = resolve_provider_config(config, provider_name)
    return provider_name, provider_type, provider_config


def test_image_path(config):
    configured_path = os.getenv("PERF_VLLM_IMAGE") or config.get(
        "module_test", {}
    ).get("vllm_image") or "docs/images/demo.jpg"

    image_path = Path(configured_path).expanduser()
    if not image_path.is_absolute():
        repository_root = Path(__file__).resolve().parents[3]
        image_path = repository_root / image_path
    if not image_path.is_file():
        raise ValueError(f"VLLM test image does not exist: {image_path}")
    if image_path.stat().st_size == 0:
        raise ValueError(f"VLLM test image is empty: {image_path}")
    return image_path


def test_question(config):
    question = os.getenv("PERF_VLLM_QUESTION") or config.get(
        "module_test", {}
    ).get("vllm_question")
    question = str(question or "Describe this image briefly.").strip()
    if not question:
        raise ValueError("The VLLM test question must not be empty")
    return question


async def run_benchmark(config):
    provider_name, provider_type, provider_config = selected_provider(config)
    image_path = test_image_path(config)
    image_base64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    question = test_question(config)
    runs = positive_int_from_env("PERF_RUNS", 5)
    timeout = positive_int_from_env("PERF_TIMEOUT_SECONDS", 60)

    initial_usage = process_usage()
    initialization_started_at = time.perf_counter()
    provider = None
    try:
        provider = create_vllm_instance(provider_type, provider_config)
        start = getattr(provider, "start", None)
        if callable(start):
            start()
    except BaseException:
        close = getattr(provider, "close", None)
        if callable(close):
            close()
        raise
    initialization_duration = time.perf_counter() - initialization_started_at
    initialized_usage = process_usage()

    durations = []
    response_lengths = []
    failures = []
    model_name = getattr(
        provider, "model_name", provider_config.get("model_name", "default")
    )

    print(f"VLLM provider: {provider_name} ({provider_type}, {model_name})")
    print_initialization_usage(
        initialization_duration,
        initial_usage,
        initialized_usage,
    )
    print(f"Runs: {runs}; timeout per run: {timeout}s")
    print(f"Image: {image_path} ({image_path.stat().st_size} bytes)")
    print(f"Question: {question}")

    try:
        for run_number in range(1, runs + 1):
            started_at = time.perf_counter()
            try:
                response = await asyncio.wait_for(
                    asyncio.to_thread(provider.response, question, image_base64),
                    timeout=timeout,
                )
                duration = time.perf_counter() - started_at
                response = str(response or "").strip()
                if not response:
                    raise RuntimeError("Provider returned no response")

                durations.append(duration)
                response_lengths.append(len(response))
                print(
                    f"Run {run_number}/{runs}: success in {duration:.3f}s - "
                    f"{response[:100]}"
                )
            except asyncio.TimeoutError:
                duration = time.perf_counter() - started_at
                message = f"timed out after {duration:.3f}s"
                failures.append(message)
                print(f"Run {run_number}/{runs}: failed - {message}")
                print("Stopping to avoid overlapping requests from a timed-out call.")
                break
            except Exception as error:
                duration = time.perf_counter() - started_at
                message = f"{type(error).__name__}: {error} ({duration:.3f}s)"
                failures.append(message)
                print(f"Run {run_number}/{runs}: failed - {message}")
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()

    print("\nVLLM benchmark summary")
    print(f"Success rate: {len(durations)}/{runs} ({len(durations) / runs:.0%})")
    if durations:
        print(format_stats("Latency", durations))
        print(
            "Mean response length: "
            f"{statistics.mean(response_lengths):.0f} characters"
        )
    print_benchmark_usage(initialized_usage)
    if failures:
        print("Failures:")
        for failure in failures:
            print(f"- {failure}")


async def main():
    config = await load_config()
    await run_benchmark(config)


if __name__ == "__main__":
    asyncio.run(main())
