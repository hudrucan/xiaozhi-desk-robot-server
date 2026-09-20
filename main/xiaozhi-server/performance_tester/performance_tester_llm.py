import asyncio
import logging
import math
import os
import statistics
import time

from config.settings import load_config
from core.utils.llm import create_instance as create_llm_instance
from core.utils.prompt_manager import PromptManager


logging.basicConfig(level=logging.WARNING)

description = "Benchmark the selected LLM provider"


def get_setting(name, default):
    value = int(os.getenv(name, default))
    if value < 1:
        raise ValueError(f"{name} must be at least 1")
    return value


def collect_response(provider, messages):
    started_at = time.perf_counter()
    first_token_time = None
    response_parts = []

    for chunk in provider.response("performance-test", messages):
        if not chunk:
            continue
        if first_token_time is None:
            first_token_time = time.perf_counter() - started_at
        response_parts.append(str(chunk))

    total_time = time.perf_counter() - started_at
    response = "".join(response_parts)
    if first_token_time is None or not response.strip():
        raise RuntimeError("Provider returned no response")
    return first_token_time, total_time, response


def format_stats(label, values):
    ordered = sorted(values)
    p95_index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return (
        f"{label}: min {min(values):.3f}s, "
        f"median {statistics.median(values):.3f}s, "
        f"mean {statistics.mean(values):.3f}s, "
        f"p95 {ordered[p95_index]:.3f}s, max {max(values):.3f}s"
    )


async def main():
    config = await load_config()
    provider_name = config.get("selected_module", {}).get("LLM")
    provider_config = config.get("LLM", {}).get(provider_name)
    if not provider_name or provider_config is None:
        raise ValueError("The selected LLM provider is not configured")

    provider_type = provider_config.get("type", provider_name)
    provider = create_llm_instance(provider_type, provider_config)
    prompt_manager = PromptManager(config)
    system_prompt = prompt_manager.build_enhanced_prompt(
        config.get("prompt", ""),
        "performance-test",
    )
    default_prompts = config.get("module_test", {}).get("test_sentences", [])
    test_prompt = os.getenv("PERF_LLM_PROMPT") or (
        default_prompts[0] if default_prompts else "Hello."
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": test_prompt},
    ]
    runs = get_setting("PERF_RUNS", 5)
    timeout = get_setting("PERF_TIMEOUT_SECONDS", 60)
    first_token_times = []
    total_times = []

    print(f"LLM provider: {provider_name} ({provider_type})")
    print(f"Runs: {runs}; timeout per run: {timeout}s")
    print(f"Prompt: {test_prompt}")

    for run_number in range(1, runs + 1):
        try:
            first_token, total, response = await asyncio.wait_for(
                asyncio.to_thread(collect_response, provider, messages),
                timeout=timeout,
            )
            first_token_times.append(first_token)
            total_times.append(total)
            print(
                f"Run {run_number}/{runs}: first token {first_token:.3f}s, "
                f"total {total:.3f}s - {response[:100]}"
            )
        except asyncio.TimeoutError:
            print(f"Run {run_number}/{runs}: failed - timed out after {timeout}s")
            print("Stopping to avoid overlapping requests from a timed-out provider call.")
            break
        except Exception as error:
            print(
                f"Run {run_number}/{runs}: failed - "
                f"{type(error).__name__}: {error}"
            )

    print("\nLLM benchmark summary")
    print(f"Success rate: {len(total_times)}/{runs} ({len(total_times) / runs:.0%})")
    if total_times:
        print(format_stats("First token", first_token_times))
        print(format_stats("Total response", total_times))


if __name__ == "__main__":
    asyncio.run(main())
