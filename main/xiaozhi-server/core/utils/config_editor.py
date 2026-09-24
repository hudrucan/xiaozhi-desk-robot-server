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
    "context_providers",
    "delete_audio",
    "device_mcp_tool_cache",
    "dump_full_llm_request",
    "enable_direct_answer_tool",
    "enable_greeting",
    "enable_stop_tts_notify",
    "enable_turn_metrics",
    "enable_wakeup_words_response_cache",
    "enable_websocket_ping",
    "end_prompt",
    "exit_commands",
    "exit_farewell",
    "llm_request_dump_file",
    "log",
    "mcp_endpoint",
    "module_test",
    "plugins",
    "prompt",
    "prompt_template",
    "selected_module",
    "server",
    "stop_tts_notify_voice",
    "system_error_response",
    "tool_error_response",
    "tool_call_timeout",
    "tool_timeout_response",
    "tts_audio_send_delay",
    "tts_timeout",
    "voiceprint",
    "wakeup_greeting",
    "wakeup_words",
    "xiaozhi",
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
DIAGNOSTIC_THRESHOLD_KEYS = {
    "first_audio",
    "llm_first",
    "resumed_llm_first",
    "tool",
    "total",
    "tts_first",
}


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


def _drop_blank_secrets(value, existing=None):
    if isinstance(value, list):
        existing_items = existing if isinstance(existing, list) else []

        def matching_existing(item, index):
            if isinstance(item, Mapping):
                for identity_key in ("url", "name", "id"):
                    identity = item.get(identity_key)
                    if identity in (None, ""):
                        continue
                    return next(
                        (
                            candidate
                            for candidate in existing_items
                            if isinstance(candidate, Mapping)
                            and candidate.get(identity_key) == identity
                        ),
                        None,
                    )
            return existing_items[index] if index < len(existing_items) else None

        return [
            _drop_blank_secrets(
                item,
                matching_existing(item, index),
            )
            for index, item in enumerate(value)
        ]
    if not isinstance(value, Mapping):
        return value

    cleaned = {}
    for key, child in value.items():
        if _is_secret(key) and (child is None or str(child).strip() == ""):
            if isinstance(existing, Mapping) and _is_configured_secret(
                existing.get(key)
            ):
                cleaned[key] = existing[key]
            continue
        existing_child = existing.get(key) if isinstance(existing, Mapping) else None
        cleaned[key] = _drop_blank_secrets(child, existing_child)
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
            current_effective = merge_configs(default_config, local_config)
            safe_patch = _drop_blank_secrets(patch, current_effective)
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

        settings_config = server.get("settings", {})
        if not isinstance(settings_config, Mapping):
            raise ValueError("server.settings must be an object")
        diagnostics_config = settings_config.get("diagnostics", {})
        if not isinstance(diagnostics_config, Mapping):
            raise ValueError("server.settings.diagnostics must be an object")
        diagnostic_thresholds = diagnostics_config.get("thresholds_ms", {})
        if not isinstance(diagnostic_thresholds, Mapping):
            raise ValueError(
                "server.settings.diagnostics.thresholds_ms must be an object"
            )
        unsupported_thresholds = sorted(
            set(diagnostic_thresholds) - DIAGNOSTIC_THRESHOLD_KEYS
        )
        if unsupported_thresholds:
            raise ValueError(
                "Unsupported diagnostic threshold: "
                f"{unsupported_thresholds[0]}"
            )
        for key, value in diagnostic_thresholds.items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(
                    f"Diagnostic threshold {key} must be an integer"
                )
            if value < 1:
                raise ValueError(
                    f"Diagnostic threshold {key} must be at least 1 ms"
                )

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
