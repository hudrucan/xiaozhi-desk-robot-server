import atexit
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import httpx

from config.logger import setup_logging
from core.providers.llm.openai.openai import LLMProvider as OpenAICompatibleProvider


TAG = __name__
logger = setup_logging()


class LLMProvider(OpenAICompatibleProvider):
    """OpenAI-compatible llama.cpp provider with an optional managed server."""

    # llama.cpp buffers parsed tool arguments until it has a complete call. Its
    # virtual direct-answer call therefore removes the latency benefit of text
    # streaming, while real tools continue to work through the normal path.
    supports_direct_answer_tool = False

    def __init__(self, config):
        llama_config = dict(config)
        process_config = llama_config.get("process") or {}

        self._process = None
        self._log_handle = None
        self._managed = bool(process_config.get("managed", True))
        self._shutdown_timeout = float(
            process_config.get("shutdown_timeout", 10)
        )
        self._max_history_messages = max(
            0, int(llama_config.get("max_history_messages", 8))
        )

        host = str(process_config.get("host", "127.0.0.1"))
        port = int(process_config.get("port", 8080))
        base_url = llama_config.get("base_url") or f"http://{host}:{port}/v1"
        llama_config["base_url"] = base_url.rstrip("/")
        llama_config.setdefault("api_key", "local")

        if self._managed:
            # Direct provider users such as performance_tester.py do not own the
            # application shutdown hook, so keep a final idempotent safeguard.
            atexit.register(self.close)
            self._start_server(process_config, host, port, llama_config)

        try:
            super().__init__(llama_config)
        except BaseException:
            self.close()
            raise

    def _start_server(self, process_config, host, port, llama_config):
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
                "LlamaCppLLM process requires exactly one of hf_model or model_path"
            )

        health_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
        health_url = f"http://{health_host}:{port}/health"
        if self._endpoint_is_open(health_url):
            raise RuntimeError(
                f"Port {host}:{port} is already serving HTTP; managed llama.cpp "
                "will not take ownership of an existing process"
            )

        command = [
            executable,
            "--host",
            host,
            "--port",
            str(port),
            "--alias",
            str(llama_config.get("model_name")),
            "--ctx-size",
            str(process_config.get("context_size", 4096)),
            "--gpu-layers",
            str(process_config.get("gpu_layers", "all")),
            "--parallel",
            str(process_config.get("parallel", 1)),
            "--jinja",
            "--no-webui",
        ]
        chat_template_kwargs = process_config.get("chat_template_kwargs")
        if chat_template_kwargs:
            if not isinstance(chat_template_kwargs, dict):
                raise ValueError(
                    "LlamaCppLLM process.chat_template_kwargs must be a mapping"
                )
            command.extend(
                [
                    "--chat-template-kwargs",
                    json.dumps(chat_template_kwargs, separators=(",", ":")),
                ]
            )

        reasoning = process_config.get("reasoning")
        if reasoning:
            command.extend(["--reasoning", str(reasoning)])
        if hf_model:
            command.extend(["-hf", str(hf_model)])
        else:
            resolved_model_path = Path(model_path).expanduser().resolve()
            if not resolved_model_path.is_file():
                raise FileNotFoundError(
                    f"llama.cpp model file not found: {resolved_model_path}"
                )
            command.extend(["--model", str(resolved_model_path)])

        sleep_idle_seconds = process_config.get("sleep_idle_seconds")
        if sleep_idle_seconds is not None:
            command.extend(["--sleep-idle-seconds", str(sleep_idle_seconds)])

        extra_args = process_config.get("extra_args", [])
        if not isinstance(extra_args, list) or not all(
            isinstance(value, (str, int, float)) for value in extra_args
        ):
            raise ValueError("LlamaCppLLM process.extra_args must be a list")
        command.extend(str(value) for value in extra_args)

        cache_reuse = int(process_config.get("cache_reuse", 0))
        if cache_reuse > 0:
            command.extend(["--cache-reuse", str(cache_reuse)])

        log_file = process_config.get("log_file", "tmp/llama-server.log")
        if log_file:
            log_path = Path(log_file).expanduser()
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_handle = log_path.open("a", encoding="utf-8")
            output = self._log_handle
        else:
            output = subprocess.DEVNULL

        logger.bind(tag=TAG).info(
            f"Starting managed llama.cpp model: {llama_config.get('model_name')}"
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

    def normalize_dialogue(self, dialogue):
        """Keep the cacheable prefix stable and bound local-model history."""
        normalized = super().normalize_dialogue(dialogue)

        # Volatile context inside the system message invalidates the expensive
        # system + tool-schema KV prefix. Keep a stable marker there and attach
        # the actual context to the latest user request instead.
        request_context = []
        for msg in normalized:
            if msg.get("role") != "system" or not isinstance(msg.get("content"), str):
                continue
            for tag in ("context", "memory"):
                pattern = rf"<{tag}>.*?</{tag}>"
                match = re.search(pattern, msg["content"], flags=re.DOTALL)
                if not match:
                    continue
                request_context.append(match.group(0))
                msg["content"] = re.sub(
                    pattern,
                    f"<{tag}>Supplied with the latest user message.</{tag}>",
                    msg["content"],
                    count=1,
                    flags=re.DOTALL,
                )
            break

        if request_context:
            latest_user = next(
                (msg for msg in reversed(normalized) if msg.get("role") == "user"),
                None,
            )
            if latest_user is not None and isinstance(latest_user.get("content"), str):
                latest_user["content"] += "\n\n" + "\n".join(request_context)

        if self._max_history_messages <= 0:
            return normalized

        system_messages = [msg for msg in normalized if msg.get("role") == "system"]
        history = [msg for msg in normalized if msg.get("role") != "system"]
        if len(history) <= self._max_history_messages:
            return normalized

        history = history[-self._max_history_messages:]
        # Never begin a trimmed request with an orphan assistant/tool message.
        first_user = next(
            (index for index, msg in enumerate(history) if msg.get("role") == "user"),
            None,
        )
        if first_user is not None:
            history = history[first_user:]

        return system_messages + history

    def prewarm(self, system_prompt, functions):
        """Populate llama.cpp's prompt cache without producing user output."""
        dialogue = self.normalize_dialogue(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "Cache this assistant context."},
            ]
        )
        response = self.client.chat.completions.create(
            model=self.model_name,
            messages=dialogue,
            tools=functions,
            stream=False,
            max_tokens=1,
            temperature=0,
        )
        return response is not None

    @staticmethod
    def _endpoint_is_open(health_url):
        try:
            response = httpx.get(health_url, timeout=0.5)
            return response.status_code in {200, 503}
        except httpx.HTTPError:
            return False

    def _wait_until_ready(self, health_url, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            return_code = self._process.poll()
            if return_code is not None:
                raise RuntimeError(
                    f"llama-server exited during startup with code {return_code}"
                )
            try:
                response = httpx.get(health_url, timeout=1.0)
                if response.status_code == 200:
                    logger.bind(tag=TAG).info("Managed llama.cpp server is ready")
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        raise TimeoutError(
            f"llama-server was not ready after {timeout:.1f} seconds"
        )

    def close(self):
        """Stop only the llama-server process created by this provider."""
        process = self._process
        self._process = None
        if process is not None and process.poll() is None:
            logger.bind(tag=TAG).info("Stopping managed llama.cpp server")
            process.terminate()
            try:
                process.wait(timeout=self._shutdown_timeout)
            except subprocess.TimeoutExpired:
                logger.bind(tag=TAG).warning(
                    "Managed llama.cpp server did not stop in time; killing it"
                )
                process.kill()
                process.wait()

        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None
