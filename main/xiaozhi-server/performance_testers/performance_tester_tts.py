import asyncio
import io
import logging
import math
import os
import statistics
import time
import wave

from config.settings import load_config
from core.utils.tts import create_instance as create_tts_instance
from performance_testers.resource_usage import (
    print_benchmark_usage,
    print_initialization_usage,
    process_usage,
)


logging.basicConfig(level=logging.WARNING)

description = "Benchmark the selected TTS provider, including failure rate"


def positive_int_from_env(name, default):
    value = int(os.getenv(name, default))
    if value < 1:
        raise ValueError(f"{name} must be at least 1")
    return value


def percentile(values, ratio):
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * ratio) - 1)
    return ordered[index]


def summarize(values):
    return {
        "min": min(values),
        "median": statistics.median(values),
        "mean": statistics.mean(values),
        "p95": percentile(values, 0.95),
        "max": max(values),
    }


def wav_duration(audio):
    try:
        with wave.open(io.BytesIO(audio), "rb") as wav_file:
            frame_rate = wav_file.getframerate()
            if frame_rate <= 0:
                return None
            return wav_file.getnframes() / frame_rate
    except (EOFError, wave.Error):
        return None


def selected_provider(config):
    provider_name = config.get("selected_module", {}).get("TTS")
    if not provider_name:
        raise ValueError("No TTS provider is selected")

    provider_config = config.get("TTS", {}).get(provider_name)
    if provider_config is None:
        raise ValueError(f"Missing configuration for TTS provider: {provider_name}")
    return provider_name, provider_config


def test_text(config):
    configured_text = config.get("module_test", {}).get("tts_text")
    if configured_text:
        return configured_text

    sentences = config.get("module_test", {}).get("test_sentences", [])
    if sentences:
        return sentences[0]
    return "This is a speech synthesis latency test."


async def close_provider(provider):
    close = getattr(provider, "close", None)
    if close is not None:
        await close()


async def run_benchmark(config):
    provider_name, provider_config = selected_provider(config)
    provider_type = provider_config.get("type", provider_name)
    initial_peak_rss, initial_cpu_time = process_usage()
    initialization_started_at = time.perf_counter()
    provider = create_tts_instance(
        provider_type,
        provider_config,
        delete_audio_file=True,
    )
    initialization_duration = time.perf_counter() - initialization_started_at
    loaded_peak_rss, loaded_cpu_time = process_usage()

    runs = positive_int_from_env("PERF_RUNS", 5)
    timeout = positive_int_from_env("PERF_TIMEOUT_SECONDS", 60)
    text = os.getenv("PERF_TTS_TEXT") or test_text(config)
    durations = []
    audio_durations = []
    realtime_factors = []
    audio_sizes = []
    failures = []

    print(f"TTS provider: {provider_name} ({provider_type})")
    print_initialization_usage(
        initialization_duration,
        (initial_peak_rss, initial_cpu_time),
        (loaded_peak_rss, loaded_cpu_time),
    )
    print(f"Runs: {runs}; timeout per run: {timeout}s")
    print(f"Text: {text}")

    try:
        for run_number in range(1, runs + 1):
            started_at = time.perf_counter()
            try:
                audio = await asyncio.wait_for(
                    provider.text_to_speak(text, None),
                    timeout=timeout,
                )
                duration = time.perf_counter() - started_at
                if not audio:
                    raise RuntimeError("Provider returned no audio")

                audio_size = len(audio)
                generated_audio_duration = wav_duration(audio)
                durations.append(duration)
                audio_sizes.append(audio_size)
                details = f"{audio_size} bytes"
                if generated_audio_duration:
                    realtime_factor = duration / generated_audio_duration
                    audio_durations.append(generated_audio_duration)
                    realtime_factors.append(realtime_factor)
                    details += (
                        f", {generated_audio_duration:.3f}s audio, "
                        f"RTF {realtime_factor:.3f}"
                    )
                print(
                    f"Run {run_number}/{runs}: success in {duration:.3f}s "
                    f"({details})"
                )
            except asyncio.TimeoutError:
                duration = time.perf_counter() - started_at
                message = f"timed out after {duration:.3f}s"
                failures.append(message)
                print(f"Run {run_number}/{runs}: failed - {message}")
            except Exception as error:
                duration = time.perf_counter() - started_at
                message = f"{type(error).__name__}: {error} ({duration:.3f}s)"
                failures.append(message)
                print(f"Run {run_number}/{runs}: failed - {message}")
    finally:
        await close_provider(provider)

    print("\nTTS benchmark summary")
    print(f"Success rate: {len(durations)}/{runs} ({len(durations) / runs:.0%})")
    if durations:
        stats = summarize(durations)
        print(
            "Latency: "
            f"min {stats['min']:.3f}s, median {stats['median']:.3f}s, "
            f"mean {stats['mean']:.3f}s, p95 {stats['p95']:.3f}s, "
            f"max {stats['max']:.3f}s"
        )
        print(f"Mean audio size: {statistics.mean(audio_sizes):.0f} bytes")
        if realtime_factors:
            rtf_stats = summarize(realtime_factors)
            print(
                "RTF: "
                f"min {rtf_stats['min']:.3f}, "
                f"median {rtf_stats['median']:.3f}, "
                f"mean {rtf_stats['mean']:.3f}, "
                f"p95 {rtf_stats['p95']:.3f}, "
                f"max {rtf_stats['max']:.3f}"
            )
            print(
                "Mean audio duration: "
                f"{statistics.mean(audio_durations):.3f}s"
            )
    print_benchmark_usage((loaded_peak_rss, loaded_cpu_time))
    if failures:
        print("Failures:")
        for failure in failures:
            print(f"- {failure}")


async def main():
    config = await load_config()
    await run_benchmark(config)


if __name__ == "__main__":
    asyncio.run(main())
