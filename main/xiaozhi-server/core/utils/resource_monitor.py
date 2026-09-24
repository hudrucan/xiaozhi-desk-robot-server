import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

try:
    import psutil
except ImportError:  # Keep the settings API available before dependencies update.
    psutil = None


class ResourceMonitor:
    """Sample the server process tree without retaining process handles."""

    def __init__(self):
        self.root_pid = os.getpid()
        self._started_at = time.monotonic()
        self._lock = threading.Lock()
        self._previous_cpu_seconds = {}
        self._previous_sample_time = None
        self._unique_memory_bytes = {}
        self._last_unique_memory_sample = None
        self._nvidia_smi = shutil.which("nvidia-smi")
        if psutil is not None:
            psutil.cpu_percent(interval=None)

    def sample(self):
        with self._lock:
            return self._sample_locked()

    def _sample_locked(self):
        sampled_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if psutil is None:
            return {
                "available": False,
                "sampled_at": sampled_at,
                "reason": "psutil is not installed",
            }

        now = time.monotonic()
        processes = self._read_process_tree()
        if not processes:
            return {
                "available": False,
                "sampled_at": sampled_at,
                "reason": "The server process tree could not be sampled",
            }
        current_cpu_seconds = {
            process["pid"]: process["cpu_seconds"] for process in processes
        }
        elapsed = (
            now - self._previous_sample_time
            if self._previous_sample_time is not None
            else None
        )

        def cpu_percent(selected_processes):
            if not elapsed or elapsed <= 0:
                return None
            cpu_delta = sum(
                max(
                    0.0,
                    process["cpu_seconds"]
                    - self._previous_cpu_seconds.get(
                        process["pid"], process["cpu_seconds"]
                    ),
                )
                for process in selected_processes
            )
            return round(cpu_delta / elapsed * 100.0, 1)

        server_processes = [
            process for process in processes if process["pid"] == self.root_pid
        ]
        model_processes = [
            process for process in processes if process["is_local_model"]
        ]

        payload = {
            "available": True,
            "sampled_at": sampled_at,
            "logical_cpu_count": os.cpu_count(),
            "server_uptime_seconds": round(time.monotonic() - self._started_at, 1),
            "system": self._read_system_usage(),
            "total": self._summarize(processes, cpu_percent(processes)),
            "server": self._summarize(
                server_processes, cpu_percent(server_processes)
            ),
            "local_models": self._summarize(
                model_processes, cpu_percent(model_processes)
            ),
            "gpu": self._read_gpu_usage({process["pid"] for process in processes}),
        }
        payload["local_models"]["active"] = bool(model_processes)
        payload["local_models"]["processes"] = [
            {"pid": process["pid"], "name": process["name"]}
            for process in model_processes
        ]

        self._previous_cpu_seconds = current_cpu_seconds
        self._previous_sample_time = now
        return payload

    def _read_process_tree(self):
        now = time.monotonic()
        refresh_unique_memory = (
            self._last_unique_memory_sample is None
            or now - self._last_unique_memory_sample >= 10
        )
        try:
            root = psutil.Process(self.root_pid)
            candidates = [root, *root.children(recursive=True)]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return []

        processes = []
        for process in candidates:
            try:
                with process.oneshot():
                    cpu_times = process.cpu_times()
                    name = process.name()
                    command = process.cmdline()
                    processes.append(
                        {
                            "pid": process.pid,
                            "name": name,
                            "cpu_seconds": cpu_times.user + cpu_times.system,
                            "memory_bytes": process.memory_info().rss,
                            "unique_memory_bytes": (
                                self._read_unique_memory(process)
                                if refresh_unique_memory
                                else self._unique_memory_bytes.get(process.pid)
                            ),
                            "is_local_model": self._is_local_model(name, command),
                        }
                    )
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        if refresh_unique_memory:
            self._unique_memory_bytes = {
                process["pid"]: process["unique_memory_bytes"]
                for process in processes
                if process["unique_memory_bytes"] is not None
            }
            self._last_unique_memory_sample = now
        return processes

    @staticmethod
    def _read_unique_memory(process):
        try:
            return process.memory_full_info().uss
        except (
            AttributeError,
            NotImplementedError,
            OSError,
            psutil.NoSuchProcess,
            psutil.AccessDenied,
        ):
            return None

    @staticmethod
    def _read_system_usage():
        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
        try:
            load_average = [round(value, 2) for value in os.getloadavg()]
        except (AttributeError, OSError):
            load_average = None
        return {
            "cpu_percent": round(psutil.cpu_percent(interval=None), 1),
            "memory_total_bytes": memory.total,
            "memory_used_bytes": memory.used,
            "memory_available_bytes": memory.available,
            "memory_percent": round(memory.percent, 1),
            "swap_used_bytes": swap.used,
            "swap_total_bytes": swap.total,
            "uptime_seconds": max(0, round(time.time() - psutil.boot_time(), 1)),
            "load_average": load_average,
        }

    @staticmethod
    def _is_local_model(name, command):
        executable = str(name).casefold()
        command_name = os.path.basename(command[0]).casefold() if command else ""
        return any(
            marker in executable or marker in command_name
            for marker in ("llama-server", "llama.cpp")
        )

    @staticmethod
    def _summarize(processes, cpu_percent):
        unique_values = [
            process["unique_memory_bytes"]
            for process in processes
            if process["unique_memory_bytes"] is not None
        ]
        return {
            "cpu_percent": cpu_percent,
            "memory_bytes": sum(
                process["memory_bytes"] for process in processes
            ),
            "unique_memory_bytes": (
                sum(unique_values) if len(unique_values) == len(processes) else None
            ),
            "process_count": len(processes),
        }

    def _read_gpu_usage(self, tracked_pids):
        if self._nvidia_smi:
            try:
                result = subprocess.run(
                    [
                        self._nvidia_smi,
                        "--query-compute-apps=pid,used_memory",
                        "--format=csv,noheader,nounits",
                    ],
                    capture_output=True,
                    check=False,
                    text=True,
                    timeout=1.0,
                )
                if result.returncode == 0:
                    memory_mb = 0.0
                    process_count = 0
                    for line in result.stdout.splitlines():
                        values = [value.strip() for value in line.split(",", 1)]
                        if len(values) != 2:
                            continue
                        try:
                            pid = int(values[0])
                            used_memory = float(values[1])
                        except ValueError:
                            continue
                        if pid in tracked_pids:
                            memory_mb += used_memory
                            process_count += 1
                    return {
                        "available": True,
                        "backend": "NVIDIA",
                        "memory_bytes": int(memory_mb * 1024 * 1024),
                        "process_count": process_count,
                    }
            except (OSError, subprocess.SubprocessError):
                pass

        if sys.platform == "darwin":
            return {
                "available": False,
                "backend": "Metal",
                "reason": (
                    "Per-process Metal usage is unavailable without privileged "
                    "system sampling; Apple GPU memory is unified with system RAM."
                ),
            }
        return {
            "available": False,
            "backend": None,
            "reason": "Per-process GPU metrics are unavailable on this system.",
        }
