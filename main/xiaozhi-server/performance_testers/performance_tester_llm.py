import asyncio
import logging
import math
import os
import random
import statistics
import time

from config.settings import load_config
from core.utils.llm import create_instance as create_llm_instance
from core.utils.prompt_manager import PromptManager
from performance_testers.resource_usage import (
    print_benchmark_usage,
    print_initialization_usage,
    process_usage,
)


logging.basicConfig(level=logging.WARNING)

description = "Benchmark the selected LLM provider"


def get_setting(name, default):
    value = int(os.getenv(name, default))
    if value < 1:
        raise ValueError(f"{name} must be at least 1")
    return value


def select_prompts(config, count, seed):
    override = os.getenv("PERF_LLM_PROMPT")
    prompts = [override] if override else config.get("module_test", {}).get(
        "test_sentences", []
    )
    prompts = [str(prompt).strip() for prompt in prompts if str(prompt).strip()]
    if not prompts:
        prompts = ["Hello."]

    rng = random.Random(seed)
    selected = []
    while len(selected) < count:
        batch = prompts.copy()
        rng.shuffle(batch)
        selected.extend(batch)
    return prompts, selected[:count]


def collect_response(provider, messages, functions=None):
    started_at = time.perf_counter()
    first_output = None
    response_parts = []
    tool_names = []

    if functions is not None:
        chunks = provider.response_with_functions(
            "performance-test", messages, functions=functions
        )
    else:
        chunks = provider.response("performance-test", messages)

    for chunk in chunks:
        if functions is not None:
            content, tool_calls = chunk
            if tool_calls:
                if first_output is None:
                    first_output = time.perf_counter() - started_at
                for tool_call in tool_calls:
                    function = getattr(tool_call, "function", None)
                    name = getattr(function, "name", None)
                    if name and name not in tool_names:
                        tool_names.append(name)
            chunk = content
        if not chunk:
            continue
        if first_output is None:
            first_output = time.perf_counter() - started_at
        response_parts.append(str(chunk))

    total = time.perf_counter() - started_at
    response = "".join(response_parts).strip()
    if not response and tool_names:
        response = f"Tool call: {', '.join(tool_names)}"
    if first_output is None or not response:
        raise RuntimeError("Provider returned no response")
    search_used = (
        getattr(provider, "last_native_google_search_used", None)
        if functions is not None
        else None
    )
    return first_output, total, response, search_used


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
    use_native_search = bool(provider_config.get("native_google_search", False))
    initial_usage = process_usage()
    initialization_started_at = time.perf_counter()
    provider = create_llm_instance(provider_type, provider_config)
    initialization_duration = time.perf_counter() - initialization_started_at
    initialized_usage = process_usage()
    system_prompt = None
    functions = [] if use_native_search else None
    prewarm_duration = None
    prewarm_tool_count = 0
    prewarm = getattr(provider, "prewarm", None)
    if callable(prewarm):
        from core.providers.llm.llama_cpp.prewarm import build_prewarm_request

        prewarm_request = build_prewarm_request(
            config,
            device_id="performance-test",
        )
        if prewarm_request is not None:
            system_prompt, functions = prewarm_request
            prewarm_tool_count = len(functions)
            prewarm_started_at = time.perf_counter()
            await asyncio.to_thread(prewarm, system_prompt, functions)
            prewarm_duration = time.perf_counter() - prewarm_started_at
    if system_prompt is None:
        system_prompt = PromptManager(config).build_enhanced_prompt(
            config.get("prompt", ""),
            "performance-test",
            emoji_enabled=True,
        )
    runs = get_setting("PERF_RUNS", 5)
    timeout = get_setting("PERF_TIMEOUT_SECONDS", 60)
    seed = int(os.getenv("PERF_LLM_SEED", 42))
    prompt_set, prompts = select_prompts(config, runs, seed)
    first_output_times = []
    total_times = []
    search_usage = []

    model_name = provider_config.get("model_name", "default")
    print(f"LLM provider: {provider_name} ({provider_type}, {model_name})")
    print_initialization_usage(
        initialization_duration,
        initial_usage,
        initialized_usage,
    )
    print(f"Samples: {runs}; timeout per sample: {timeout}s")
    print(f"Prompt set: {len(prompt_set)}; seed: {seed}")
    print(f"System prompt: {len(system_prompt)} characters")
    if prewarm_duration is not None:
        print(
            f"Prompt prewarm: {prewarm_duration:.3f}s "
            f"({prewarm_tool_count} tool schemas)"
        )
    elif callable(prewarm):
        print("Prompt prewarm: disabled or no cached device tools")
    print(
        "Native Google Search path: "
        f"{'enabled' if use_native_search else 'disabled'}"
    )

    for sample_number, prompt in enumerate(prompts, 1):
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]
        try:
            first_output, total, response, search_used = await asyncio.wait_for(
                asyncio.to_thread(
                    collect_response,
                    provider,
                    messages,
                    functions,
                ),
                timeout=timeout,
            )
            first_output_times.append(first_output)
            total_times.append(total)
            if search_used is not None:
                search_usage.append(search_used)
            print(f"\nSample {sample_number}/{runs}: {prompt}")
            print(
                f"First output {first_output:.3f}s, total {total:.3f}s - "
                f"{response[:100]}"
            )
            if search_used is not None:
                print(f"Search used: {'yes' if search_used else 'no'}")
        except asyncio.TimeoutError:
            print(f"\nSample {sample_number}/{runs}: timed out after {timeout}s")
            print("Stopping to avoid overlapping requests from a timed-out call.")
            break
        except Exception as error:
            print(
                f"\nSample {sample_number}/{runs}: failed - "
                f"{type(error).__name__}: {error}"
            )

    print("\nLLM benchmark summary")
    print(f"Success rate: {len(total_times)}/{runs} ({len(total_times) / runs:.0%})")
    if total_times:
        print(format_stats("First output", first_output_times))
        print(format_stats("Total response", total_times))
    if search_usage:
        print(f"Google Search used: {sum(search_usage)}/{len(search_usage)} samples")
    print_benchmark_usage(initialized_usage)


if __name__ == "__main__":
    asyncio.run(main())
