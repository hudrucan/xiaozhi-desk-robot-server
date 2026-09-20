import math
import os
import statistics
import wave
from abc import ABC, abstractmethod
from pathlib import Path


class ASRBenchmarkBase(ABC):
    def __init__(self, provider_name, provider_config):
        self.provider_name = provider_name
        self.provider_config = provider_config
        self.audio_path = self.find_audio_file()
        self.pcm_data = self.load_pcm(self.audio_path)
        self.runs = self.get_setting("PERF_RUNS", 5)
        self.timeout = self.get_setting("PERF_TIMEOUT_SECONDS", 60)

    @abstractmethod
    async def run(self):
        pass

    @staticmethod
    def get_setting(name, default):
        value = int(os.getenv(name, default))
        if value < 1:
            raise ValueError(f"{name} must be at least 1")
        return value

    @staticmethod
    def load_pcm(audio_path):
        with wave.open(audio_path, "rb") as audio_file:
            if audio_file.getnchannels() != 1:
                raise ValueError("ASR test audio must be mono")
            if audio_file.getsampwidth() != 2:
                raise ValueError("ASR test audio must use 16-bit samples")
            if audio_file.getframerate() != 16000:
                raise ValueError("ASR test audio must use a 16000 Hz sample rate")
            return audio_file.readframes(audio_file.getnframes())

    @classmethod
    def find_audio_file(cls):
        configured_path = os.getenv("PERF_ASR_AUDIO")
        if configured_path:
            return configured_path

        assets_dir = Path(__file__).resolve().parents[2] / "config" / "assets"
        candidates = []
        for path in assets_dir.glob("*.wav"):
            try:
                cls.load_pcm(str(path))
                candidates.append(path)
            except (ValueError, wave.Error):
                continue
        if not candidates:
            raise ValueError("No compatible WAV test audio found; set PERF_ASR_AUDIO")
        return str(max(candidates, key=lambda path: path.stat().st_size))

    @staticmethod
    def format_stats(label, values):
        ordered = sorted(values)
        p95_index = max(0, math.ceil(len(ordered) * 0.95) - 1)
        return (
            f"{label}: min {min(values):.3f}s, "
            f"median {statistics.median(values):.3f}s, "
            f"mean {statistics.mean(values):.3f}s, "
            f"p95 {ordered[p95_index]:.3f}s, max {max(values):.3f}s"
        )

    def print_header(self, provider_detail):
        print(f"ASR provider: {self.provider_name} ({provider_detail})")
        print(f"Audio: {self.audio_path}")
        print(f"Runs: {self.runs}; timeout per run: {self.timeout}s")
