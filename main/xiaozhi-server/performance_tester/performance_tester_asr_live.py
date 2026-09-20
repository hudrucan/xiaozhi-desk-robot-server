import asyncio
import contextlib
import logging
import math
import os
import statistics
import time
import wave

from google import genai
from google.genai import types

from config.settings import load_config


logging.basicConfig(level=logging.WARNING)

description = "Benchmark the selected Gemini Live ASR provider"


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
            load_pcm(path)
            candidates.append(path)
        except (ValueError, wave.Error):
            continue
    if not candidates:
        raise ValueError("No compatible WAV test audio found; set PERF_ASR_AUDIO")
    return max(candidates, key=os.path.getsize)


def live_config(provider_config):
    language = provider_config.get("language", "auto")
    language_codes = [] if language == "auto" else [language]
    mode = str(provider_config.get("mode", "VERBATIM")).upper()
    return types.LiveConnectConfig(
        response_modalities=["TEXT"],
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                disabled=True
            )
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(
            language_codes=language_codes,
            mode=mode,
        ),
    )


async def receive_final(session, turn_started_at, activity_ending):
    first_interim = None
    async for response in session.receive():
        server_content = response.server_content
        if not server_content:
            continue
        interim = server_content.interim_input_transcription
        if interim and interim.text and first_interim is None:
            first_interim = time.perf_counter() - turn_started_at
        final = server_content.input_transcription
        if final and final.text and activity_ending.is_set():
            return final.text.strip(), first_interim, time.perf_counter()
    raise RuntimeError("Gemini Live session ended before returning a transcript")


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
    provider_name = config.get("selected_module", {}).get("ASR")
    provider_config = config.get("ASR", {}).get(provider_name)
    if not provider_name or provider_config is None:
        raise ValueError("The selected ASR provider is not configured")
    if provider_config.get("type") != "gemini_live":
        raise ValueError("The selected ASR provider is not Gemini Live ASR")

    audio_path = find_audio_file()
    pcm_data = load_pcm(audio_path)
    runs = get_setting("PERF_RUNS", 5)
    timeout = get_setting("PERF_TIMEOUT_SECONDS", 60)
    chunk_ms = get_setting("PERF_AUDIO_CHUNK_MS", 120)
    chunk_bytes = chunk_ms * 32
    client = genai.Client(api_key=provider_config.get("api_key"))
    model_name = provider_config.get(
        "model_name", "gemini-3.5-transcribe-live"
    )

    first_interim_times = []
    finalization_times = []
    total_times = []

    print(f"ASR provider: {provider_name} ({model_name})")
    print(f"Audio: {audio_path}")
    print(f"Runs: {runs}; timeout per run: {timeout}s")

    connect_started_at = time.perf_counter()
    async with client.aio.live.connect(
        model=model_name,
        config=live_config(provider_config),
    ) as session:
        connect_time = time.perf_counter() - connect_started_at
        print(f"Live session connected in {connect_time:.3f}s")

        for run_number in range(1, runs + 1):
            turn_started_at = time.perf_counter()
            activity_ending = asyncio.Event()
            receive_task = asyncio.create_task(
                receive_final(session, turn_started_at, activity_ending)
            )
            try:
                await session.send_realtime_input(
                    activity_start=types.ActivityStart()
                )
                for offset in range(0, len(pcm_data), chunk_bytes):
                    chunk = pcm_data[offset : offset + chunk_bytes]
                    await session.send_realtime_input(
                        audio=types.Blob(
                            data=chunk,
                            mime_type="audio/pcm;rate=16000",
                        )
                    )
                    await asyncio.sleep(len(chunk) / 32000)

                speech_ended_at = time.perf_counter()
                activity_ending.set()
                await session.send_realtime_input(
                    activity_end=types.ActivityEnd()
                )
                transcript, first_interim, final_received_at = await asyncio.wait_for(
                    receive_task,
                    timeout=timeout,
                )
                finalization_time = final_received_at - speech_ended_at
                total_time = final_received_at - turn_started_at
                finalization_times.append(finalization_time)
                total_times.append(total_time)
                if first_interim is not None:
                    first_interim_times.append(first_interim)
                print(
                    f"Run {run_number}/{runs}: final after speech end "
                    f"{finalization_time:.3f}s, total {total_time:.3f}s - "
                    f"{transcript[:100]}"
                )
            except Exception as error:
                receive_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await receive_task
                print(
                    f"Run {run_number}/{runs}: failed - "
                    f"{type(error).__name__}: {error}"
                )
                break

    await client.aio.aclose()

    print("\nGemini Live ASR benchmark summary")
    print(f"Success rate: {len(finalization_times)}/{runs}")
    if first_interim_times:
        print(format_stats("First interim", first_interim_times))
    if finalization_times:
        print(format_stats("Final after speech end", finalization_times))
        print(format_stats("Total turn", total_times))


if __name__ == "__main__":
    asyncio.run(main())
