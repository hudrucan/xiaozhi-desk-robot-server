import os
import shutil
import tempfile
import threading
from collections.abc import Mapping

import yaml

from config.config_loader import get_project_dir, merge_configs, read_config


EDITABLE_ROOTS = {
    "ASR",
    "Intent",
    "LLM",
    "Memory",
    "TTS",
    "VAD",
    "VLLM",
    "asr_audio_queue_max_frames",
    "asr_min_audio_ms",
    "close_connection_no_voice_time",
    "delete_audio",
    "enable_direct_answer_tool",
    "enable_greeting",
    "enable_stop_tts_notify",
    "enable_turn_metrics",
    "enable_wakeup_words_response_cache",
    "enable_websocket_ping",
    "end_prompt",
    "exit_farewell",
    "log",
    "mcp_endpoint",
    "plugins",
    "prompt",
    "prompt_template",
    "selected_module",
    "server",
    "stop_tts_notify_voice",
    "system_error_response",
    "tool_call_timeout",
    "tts_audio_send_delay",
    "tts_timeout",
    "voiceprint",
    "wakeup_greeting",
    "wakeup_words",
}

SECRET_NAMES = {
    "access_key",
    "access_key_secret",
    "access_token",
    "api_key",
    "auth_key",
    "authorization",
    "client_secret",
    "mcp_endpoint",
    "mqtt_signature_key",
    "password",
    "personal_access_token",
    "private_key",
    "secret",
    "secret_key",
    "token",
}

PROVIDER_GROUPS = ("VAD", "ASR", "LLM", "VLLM", "TTS", "Memory", "Intent")


def _is_secret(key):
    normalized = str(key).lower()
    return normalized in SECRET_NAMES or normalized.endswith(("_token", "_secret"))


def _is_configured_secret(value):
    if value is None:
        return False
    text = str(value).strip().lower()
    if not text:
        return False
    return not text.startswith("your_") and "你的" not in text


def _public_copy(value, path=(), configured_secrets=None):
    if configured_secrets is None:
        configured_secrets = []

    if isinstance(value, Mapping):
        result = {}
        for key, child in value.items():
            child_path = (*path, str(key))
            if _is_secret(key):
                result[key] = ""
                if _is_configured_secret(child):
                    configured_secrets.append(".".join(child_path))
            else:
                result[key] = _public_copy(child, child_path, configured_secrets)
        return result
    if isinstance(value, list):
        return [
            _public_copy(item, (*path, str(index)), configured_secrets)
            for index, item in enumerate(value)
        ]
    return value


def _drop_blank_secrets(value):
    if not isinstance(value, Mapping):
        return value

    cleaned = {}
    for key, child in value.items():
        if _is_secret(key) and (child is None or str(child).strip() == ""):
            continue
        cleaned[key] = _drop_blank_secrets(child)
    return cleaned


class ConfigEditor:
    """Read and atomically update the local configuration override."""

    def __init__(self):
        project_dir = get_project_dir()
        self.default_path = os.path.join(project_dir, "config.yaml")
        self.local_path = os.path.join(project_dir, "data", ".config.yaml")
        self.backup_path = f"{self.local_path}.backup"
        self._lock = threading.Lock()

    def read_public(self):
        default_config = read_config(self.default_path)
        local_config = read_config(self.local_path)
        effective_config = merge_configs(default_config, local_config)
        editable_config = {
            key: effective_config[key]
            for key in EDITABLE_ROOTS
            if key in effective_config
        }
        configured_secrets = []
        public_config = _public_copy(
            editable_config, configured_secrets=configured_secrets
        )
        return {
            "config": public_config,
            "configured_secrets": configured_secrets,
            "config_path": "data/.config.yaml",
        }

    def update(self, patch):
        if not isinstance(patch, Mapping):
            raise ValueError("config must be an object")

        unsupported = sorted(set(patch) - EDITABLE_ROOTS)
        if unsupported:
            raise ValueError(f"Unsupported configuration section: {unsupported[0]}")

        with self._lock:
            default_config = read_config(self.default_path)
            local_config = read_config(self.local_path)
            safe_patch = _drop_blank_secrets(patch)
            updated_local = merge_configs(local_config, safe_patch)
            effective_config = merge_configs(default_config, updated_local)
            self._validate(effective_config)
            self._write_atomic(updated_local)

        return self.read_public()

    def _validate(self, config):
        selected = config.get("selected_module")
        if not isinstance(selected, Mapping):
            raise ValueError("selected_module must be an object")

        for group in PROVIDER_GROUPS:
            provider = selected.get(group)
            if not provider:
                continue
            available = config.get(group, {})
            if not isinstance(available, Mapping) or provider not in available:
                raise ValueError(f"Unknown {group} provider: {provider}")

        server = config.get("server", {})
        for key in ("port", "http_port"):
            value = int(server.get(key, 0))
            if not 1 <= value <= 65535:
                raise ValueError(f"server.{key} must be between 1 and 65535")

        log_level = str(config.get("log", {}).get("log_level", "INFO")).upper()
        if log_level not in {"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR"}:
            raise ValueError("log.log_level is not supported")

        if int(config.get("asr_min_audio_ms", 300)) < 0:
            raise ValueError("asr_min_audio_ms must not be negative")
        if int(config.get("asr_audio_queue_max_frames", 200)) < 1:
            raise ValueError("asr_audio_queue_max_frames must be at least 1")

    def _write_atomic(self, config):
        directory = os.path.dirname(self.local_path)
        os.makedirs(directory, exist_ok=True)
        if os.path.exists(self.local_path):
            shutil.copy2(self.local_path, self.backup_path)

        file_descriptor, temporary_path = tempfile.mkstemp(
            prefix=".config.", suffix=".tmp", dir=directory, text=True
        )
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as file:
                yaml.safe_dump(
                    config,
                    file,
                    allow_unicode=True,
                    default_flow_style=False,
                    sort_keys=False,
                )
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, self.local_path)
        finally:
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)
