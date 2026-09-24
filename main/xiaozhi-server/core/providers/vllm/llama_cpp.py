import atexit
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import httpx
import openai

from config.logger import setup_logging
from core.providers.vllm.openai import VLLMProvider as OpenAICompatibleVLLM


TAG = __name__
logger = setup_logging()


class VLLMProvider(OpenAICompatibleVLLM):
    """OpenAI-compatible vision provider backed by managed llama.cpp."""

    def __init__(self, config):
        llama_config = dict(config)
        process_config = dict(llama_config.get("process") or {})

        self._process = None
        self._log_handle = None
        self._start_lock = threading.RLock()
        self._managed = bool(process_config.get("managed", True))
        self._lazy_start = bool(process_config.get("lazy_start", True))
        self._process_config = process_config
        self._reuse_server = dict(llama_config.get("reuse_server") or {})
        self._borrowed_server = False
        self._shutdown_timeout = float(process_config.get("shutdown_timeout", 10))

        host = str(process_config.get("host", "127.0.0.1"))
        port = int(process_config.get("port", 6001))
        self._host = host
        self._port = port
        llama_config["base_url"] = str(
            llama_config.get("base_url") or f"http://{host}:{port}/v1"
        ).rstrip("/")
        llama_config.setdefault("api_key", "local")

        try:
            super().__init__(llama_config)
            self._own_base_url = self.base_url
            self._own_model_name = self.model_name
            if self._managed:
                atexit.register(self.close)
                if not self._lazy_start:
                    self._ensure_server()
        except BaseException:
            self.close()
            raise

    def response(self, question, base64_image):
        self._ensure_server()
        return super().response(question, base64_image)

    def start(self):
        """Explicitly load the model for benchmarks while production stays lazy."""
        self._ensure_server()

    def _ensure_server(self):
        if not self._managed:
            return
        if self._process is not None and self._process.poll() is None:
            return

        with self._start_lock:
            if self._process is not None and self._process.poll() is None:
                return
            if self._try_reuse_server():
                return
            if self._borrowed_server:
                self._configure_client(self._own_base_url, self._own_model_name)
                self._borrowed_server = False
            self._start_server()

    def _try_reuse_server(self):
        health_url = self._reuse_server.get("health_url")
        if not health_url or not self._endpoint_is_ready(health_url):
            return False
        if self._borrowed_server:
            return True

        base_url = self._reuse_server.get("base_url")
        model_name = self._reuse_server.get("model_name")
        if not base_url or not model_name:
            return False
        if not self._endpoint_serves_model(base_url, model_name):
            return False
        self._configure_client(base_url, model_name)
        self._borrowed_server = True
        logger.bind(tag=TAG).info(
            f"Reusing managed llama.cpp LLM server for vision: {model_name}"
        )
        return True

    def _configure_client(self, base_url, model_name):
        previous_client = getattr(self, "client", None)
        self.base_url = str(base_url).rstrip("/")
        self.model_name = str(model_name)
        self.client = openai.OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
        )
        close = getattr(previous_client, "close", None)
        if callable(close):
            close()

    def _start_server(self):
        process_config = self._process_config
        executable_name = str(process_config.get("executable", "llama-server"))
        executable = shutil.which(executable_name)
        if executable is None:
            raise RuntimeError(
                f"llama.cpp executable not found: {executable_name}. "
                "Install it with 'brew install llama.cpp'."
            )

        hf_model = process_config.get("hf_model")
        model_path = process_config.get("model_path")
        if bool(hf_model) == bool(model_path):
            raise ValueError(
                "LlamaCppVLLM process requires exactly one of hf_model or model_path"
            )

        health_host = (
            "127.0.0.1" if self._host in {"0.0.0.0", "::"} else self._host
        )
        health_url = f"http://{health_host}:{self._port}/health"
        if self._endpoint_is_open(health_url):
            raise RuntimeError(
                f"Port {self._host}:{self._port} is already serving HTTP; managed "
                "llama.cpp VLLM will not take ownership of an existing process"
            )

        command = [
            executable,
            "--host",
            self._host,
            "--port",
            str(self._port),
            "--alias",
            str(self.model_name),
            "--ctx-size",
            str(process_config.get("context_size", 4096)),
            "--gpu-layers",
            str(process_config.get("gpu_layers", "all")),
            "--parallel",
            str(process_config.get("parallel", 1)),
            "--jinja",
            "--no-webui",
        ]
        if hf_model:
            command.extend(["-hf", str(hf_model)])
        else:
            resolved_model_path = Path(model_path).expanduser().resolve()
            if not resolved_model_path.is_file():
                raise FileNotFoundError(
                    f"llama.cpp model file not found: {resolved_model_path}"
                )
            command.extend(["--model", str(resolved_model_path)])

        mmproj_path = process_config.get("mmproj_path")
        if mmproj_path:
            resolved_mmproj_path = Path(mmproj_path).expanduser().resolve()
            if not resolved_mmproj_path.is_file():
                raise FileNotFoundError(
                    f"llama.cpp multimodal projector not found: {resolved_mmproj_path}"
                )
            command.extend(["--mmproj", str(resolved_mmproj_path)])

        sleep_idle_seconds = process_config.get("sleep_idle_seconds")
        if sleep_idle_seconds is not None:
            command.extend(["--sleep-idle-seconds", str(sleep_idle_seconds)])

        extra_args = process_config.get("extra_args", [])
        if not isinstance(extra_args, list) or not all(
            isinstance(value, (str, int, float)) for value in extra_args
        ):
            raise ValueError("LlamaCppVLLM process.extra_args must be a list")
        command.extend(str(value) for value in extra_args)

        log_file = process_config.get("log_file", "tmp/llama-vllm-server.log")
        if log_file:
            log_path = Path(log_file).expanduser()
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_handle = log_path.open("a", encoding="utf-8")
            output = self._log_handle
        else:
            output = subprocess.DEVNULL

        logger.bind(tag=TAG).info(
            f"Starting managed llama.cpp vision model: {self.model_name}"
        )
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                env=os.environ.copy(),
            )
            self._wait_until_ready(
                health_url,
                float(process_config.get("startup_timeout", 900)),
            )
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _endpoint_is_open(health_url):
        try:
            response = httpx.get(health_url, timeout=0.5)
            return response.status_code in {200, 503}
        except httpx.HTTPError:
            return False

    @staticmethod
    def _endpoint_is_ready(health_url):
        try:
            response = httpx.get(health_url, timeout=0.5)
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    @staticmethod
    def _endpoint_serves_model(base_url, model_name):
        try:
            response = httpx.get(f"{str(base_url).rstrip('/')}/models", timeout=1.0)
            if response.status_code != 200:
                return False
            models = response.json().get("data", [])
            return any(model.get("id") == model_name for model in models)
        except (httpx.HTTPError, TypeError, ValueError):
            return False

    def _wait_until_ready(self, health_url, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            return_code = self._process.poll()
            if return_code is not None:
                raise RuntimeError(
                    "llama.cpp vision server exited during startup with code "
                    f"{return_code}"
                )
            try:
                response = httpx.get(health_url, timeout=1.0)
                if response.status_code == 200:
                    logger.bind(tag=TAG).info(
                        "Managed llama.cpp vision server is ready"
                    )
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        raise TimeoutError(
            f"llama.cpp vision server was not ready after {timeout:.1f} seconds"
        )

    def close(self):
        """Stop only the llama-server process created by this provider."""
        with self._start_lock:
            process = self._process
            self._process = None
            if process is not None and process.poll() is None:
                logger.bind(tag=TAG).info(
                    "Stopping managed llama.cpp vision server"
                )
                process.terminate()
                try:
                    process.wait(timeout=self._shutdown_timeout)
                except subprocess.TimeoutExpired:
                    logger.bind(tag=TAG).warning(
                        "Managed llama.cpp vision server did not stop in time; "
                        "killing it"
                    )
                    process.kill()
                    process.wait()

            if self._log_handle is not None:
                self._log_handle.close()
                self._log_handle = None
