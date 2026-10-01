import asyncio
import time

from core.providers.asr.sherpa_streaming import ASRProvider, _ConnectionStream
from performance_testers.resource_usage import (
    print_benchmark_usage,
    print_initialization_usage,
    process_usage,
)

from .base import ASRBenchmarkBase


class SherpaStreamingASRBenchmark(ASRBenchmarkBase):
    """Measure incremental local decode from a WAV file, without server VAD/chat."""

    async def recognize(self, provider, chunk_bytes):
        state = _ConnectionStream(provider.pre_roll_frames)
        started_at = time.perf_counter()
        try:
            # Replay PCM without artificial pacing to measure processing cost.
            # Feed/drain the real OnlineStream for each chunk; never re-decode
            # the full utterance. Exported ONNX graphs set encoder chunk size.
            for offset in range(0, len(self.pcm_data), chunk_bytes):
                await provider._run_engine(
                    provider._feed,
                    state,
                    self.pcm_data[offset : offset + chunk_bytes],
                )
            input_ended_at = time.perf_counter()
            text = await provider._run_engine(provider._finish, state)
            finished_at = time.perf_counter()
            if not text:
                raise RuntimeError("Provider returned no transcription")
            return text, finished_at - started_at, finished_at - input_ended_at
        finally:
            await provider._run_engine(provider._drop_stream, state)

    async def run(self):
        if not self.pcm_data:
            raise ValueError("ASR test audio must contain PCM samples")
        chunk_bytes = self.get_setting("PERF_AUDIO_CHUNK_MS", 120) * 32
        initial_usage = process_usage()
        initialization_started_at = time.perf_counter()
        provider = await asyncio.to_thread(ASRProvider, self.provider_config, True)
        initialization_duration = time.perf_counter() - initialization_started_at
        initialized_usage = process_usage()
        processing_times = []
        finalization_times = []
        audio_seconds = len(self.pcm_data) / 32000

        self.print_header("sherpa_streaming")
        print_initialization_usage(
            initialization_duration,
            initial_usage,
            initialized_usage,
        )
        print(
            f"Audio duration: {audio_seconds:.3f}s; unpaced incremental decode; "
            "model reused across runs; no server VAD/chat"
        )

        try:
            for run_number in range(1, self.runs + 1):
                try:
                    text, processing_time, finalization_time = await asyncio.wait_for(
                        self.recognize(provider, chunk_bytes), timeout=self.timeout
                    )
                    processing_times.append(processing_time)
                    finalization_times.append(finalization_time)
                    print(
                        f"Run {run_number}/{self.runs}: processing {processing_time:.3f}s, "
                        f"final after input end {finalization_time:.3f}s, "
                        f"RTF {processing_time / audio_seconds:.3f} - {text[:100]}"
                    )
                except asyncio.TimeoutError:
                    print(
                        f"Run {run_number}/{self.runs}: failed - "
                        f"timed out after {self.timeout}s"
                    )
                except Exception as error:
                    print(
                        f"Run {run_number}/{self.runs}: failed - "
                        f"{type(error).__name__}: {error}"
                    )
        finally:
            await provider.close()

        print("\nSherpa streaming ASR benchmark summary")
        print(f"Success rate: {len(processing_times)}/{self.runs}")
        if processing_times:
            print(self.format_stats("Processing", processing_times))
            print(self.format_stats("Final after input end", finalization_times))
        print_benchmark_usage(initialized_usage)
