import asyncio
import time

from core.utils.asr import create_instance as create_asr_instance
from performance_testers.resource_usage import (
    print_benchmark_usage,
    print_initialization_usage,
    process_usage,
)

from .base import ASRBenchmarkBase


class BatchASRBenchmark(ASRBenchmarkBase):
    async def run(self):
        provider_type = self.provider_config.get("type", self.provider_name)
        initial_usage = process_usage()
        initialization_started_at = time.perf_counter()
        provider = create_asr_instance(
            provider_type,
            self.provider_config,
            delete_audio_file=True,
        )
        initialization_duration = time.perf_counter() - initialization_started_at
        initialized_usage = process_usage()
        durations = []
        self.print_header(provider_type)
        print_initialization_usage(
            initialization_duration,
            initial_usage,
            initialized_usage,
        )

        try:
            for run_number in range(1, self.runs + 1):
                started_at = time.perf_counter()
                try:
                    text, _ = await asyncio.wait_for(
                        provider.speech_to_text_wrapper(
                            [self.pcm_data],
                            f"performance-test-{run_number}",
                        ),
                        timeout=self.timeout,
                    )
                    duration = time.perf_counter() - started_at
                    if not text:
                        raise RuntimeError("Provider returned no transcription")
                    durations.append(duration)
                    print(
                        f"Run {run_number}/{self.runs}: success in "
                        f"{duration:.3f}s - {text[:100]}"
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

        print("\nASR benchmark summary")
        print(
            f"Success rate: {len(durations)}/{self.runs} "
            f"({len(durations) / self.runs:.0%})"
        )
        if durations:
            print(self.format_stats("Latency", durations))
        print_benchmark_usage(initialized_usage)
