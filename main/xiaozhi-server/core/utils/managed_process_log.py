import re
import subprocess
import threading
from pathlib import Path

from core.utils.ui_log_buffer import ui_log_buffer


_LEVEL_PATTERN = re.compile(r"\b(trace|debug|info|warn(?:ing)?|error|fatal)\b", re.I)


def _infer_level(line):
    match = _LEVEL_PATTERN.search(line)
    if not match:
        return "INFO"
    level = match.group(1).upper()
    if level == "WARN":
        return "WARNING"
    if level == "FATAL":
        return "ERROR"
    return level


class ManagedProcessLogCapture:
    """Tee one managed subprocess stream to disk and the current UI run."""

    def __init__(self, source, log_file):
        self.source = source
        self._stream = None
        self._thread = None
        self._file = None
        if log_file:
            log_path = Path(log_file).expanduser()
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._file = log_path.open("a", encoding="utf-8")

    @staticmethod
    def popen_output_options():
        return {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
        }

    def start(self, process):
        self._stream = process.stdout
        if self._stream is None:
            return
        ui_log_buffer.clear_source(self.source)
        self._thread = threading.Thread(
            target=self._drain,
            name=f"{self.source}-log-capture",
            daemon=True,
        )
        self._thread.start()

    def _drain(self):
        try:
            for raw_line in self._stream:
                if self._file is not None:
                    self._file.write(raw_line)
                    self._file.flush()
                line = raw_line.rstrip("\r\n")
                if line:
                    ui_log_buffer.append(
                        self.source,
                        _infer_level(line),
                        line,
                        tag="llama.cpp",
                    )
        finally:
            if self._stream is not None:
                self._stream.close()
                self._stream = None

    def close(self):
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._file is not None:
            self._file.close()
            self._file = None
