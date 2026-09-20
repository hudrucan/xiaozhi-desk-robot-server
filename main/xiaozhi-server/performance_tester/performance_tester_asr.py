import asyncio
import logging
import math
import os
import statistics
import time
import wave

from config.settings import load_config
from core.utils.asr import create_instance as create_asr_instance


logging.basicConfig(level=logging.WARNING)

description = "Benchmark the selected ASR provider"


def get_setting(name, default):
    value = int(os.getenv(name, default))
    if value < 1:
        raise ValueError(f"{name} must be at least 1")
    return value


def load_pcm(audio_path):
    with wave.open(audio_path, "rb") as audio_file:
        if audio_file.getnchannels() != 1:
            raise ValueError("ASR test audio must be mono")
        if audio_file.getsampwidth() != 2:
            raise ValueError("ASR test audio must use 16-bit samples")
        if audio_file.getframerate() != 16000:
            raise ValueError("ASR test audio must use a 16000 Hz sample rate")
        return audio_file.readframes(audio_file.getnframes())


def find_audio_file():
    configured_path = os.getenv("PERF_ASR_AUDIO")
    if configured_path:
        return configured_path

    assets_dir = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "config", "assets"
    )
    candidates = []
    for name in os.listdir(assets_dir):
        path = os.path.join(assets_dir, name)
        if not name.lower().endswith(".wav"):
            continue
        try:
            with wave.open(path, "rb") as audio_file:
                compatible = (
                    audio_file.getnchannels() == 1
                    and audio_file.getsampwidth() == 2
                    and audio_file.getframerate() == 16000
                )
            if compatible:
                candidates.append(path)
        except wave.Error:
            continue
    if not candidates:
        raise ValueError("No WAV test audio found; set PERF_ASR_AUDIO")
    return max(candidates, key=os.path.getsize)


def print_summary(durations, runs):
    print("\nASR benchmark summary")
    print(f"Success rate: {len(durations)}/{runs} ({len(durations) / runs:.0%})")
    if not durations:
        return

    ordered = sorted(durations)
    p95_index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    print(
        "Latency: "
        f"min {min(durations):.3f}s, median {statistics.median(durations):.3f}s, "
        f"mean {statistics.mean(durations):.3f}s, "
        f"p95 {ordered[p95_index]:.3f}s, max {max(durations):.3f}s"
    )


async def main():
    config = await load_config()
    provider_name = config.get("selected_module", {}).get("ASR")
    provider_config = config.get("ASR", {}).get(provider_name)
    if not provider_name or provider_config is None:
        raise ValueError("The selected ASR provider is not configured")

    audio_path = find_audio_file()
    pcm_data = load_pcm(audio_path)
    runs = get_setting("PERF_RUNS", 5)
    timeout = get_setting("PERF_TIMEOUT_SECONDS", 60)
    provider_type = provider_config.get("type", provider_name)
    provider = create_asr_instance(
        provider_type,
        provider_config,
        delete_audio_file=True,
    )
    durations = []

    print(f"ASR provider: {provider_name} ({provider_type})")
    print(f"Audio: {audio_path}")
    print(f"Runs: {runs}; timeout per run: {timeout}s")

    try:
        for run_number in range(1, runs + 1):
            started_at = time.perf_counter()
            try:
                text, _ = await asyncio.wait_for(
                    provider.speech_to_text_wrapper(
                        [pcm_data],
                        f"performance-test-{run_number}",
                    ),
                    timeout=timeout,
                )
                duration = time.perf_counter() - started_at
                if not text:
                    raise RuntimeError("Provider returned no transcription")
                durations.append(duration)
                print(
                    f"Run {run_number}/{runs}: success in {duration:.3f}s - "
                    f"{text[:100]}"
                )
            except asyncio.TimeoutError:
                print(f"Run {run_number}/{runs}: failed - timed out after {timeout}s")
            except Exception as error:
                print(
                    f"Run {run_number}/{runs}: failed - "
                    f"{type(error).__name__}: {error}"
                )
    finally:
        await provider.close()

    print_summary(durations, runs)


if __name__ == "__main__":
    asyncio.run(main())
