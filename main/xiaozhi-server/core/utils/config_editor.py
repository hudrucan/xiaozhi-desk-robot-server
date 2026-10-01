import copy
import os
from collections.abc import Mapping

from config.config_loader import (
    get_project_dir,
    load_default_config,
    merge_configs,
)
from config.local_config import LocalConfigStore
from core.soundbank import normalize_soundbank_text
from core.utils.config_secrets import is_secret_name


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
    "static_soundbank",
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

PROVIDER_GROUPS = ("VAD", "ASR", "LLM", "VLLM", "TTS", "Memory", "Intent")
DIAGNOSTIC_THRESHOLD_KEYS = {
    "first_audio",
    "llm_first",
    "resumed_llm_first",
    "tool",
    "total",
    "tts_first",
}


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
            if is_secret_name(key):
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
        if is_secret_name(key) and (child is None or str(child).strip() == ""):
            if isinstance(existing, Mapping) and _is_configured_secret(
                existing.get(key)
            ):
                cleaned[key] = existing[key]
            continue
        existing_child = existing.get(key) if isinstance(existing, Mapping) else None
        cleaned[key] = _drop_blank_secrets(child, existing_child)
    return cleaned


def _merge_editor_patch(local_config, patch):
    """Merge Settings edits while replacing the complete soundbank entry map."""
    merged = merge_configs(local_config, patch)
    soundbank_patch = patch.get("static_soundbank")
    if not isinstance(soundbank_patch, Mapping) or "entries" not in soundbank_patch:
        return merged

    soundbank_config = merged.get("static_soundbank")
    soundbank_config = (
        dict(soundbank_config) if isinstance(soundbank_config, Mapping) else {}
    )
    soundbank_config["entries"] = copy.deepcopy(soundbank_patch["entries"])
    merged["static_soundbank"] = soundbank_config
    return merged


class ConfigEditor:
    """Read and atomically update the local configuration override."""

    def __init__(self):
        project_dir = get_project_dir()
        self.default_path = os.path.join(project_dir, "config.yaml")
        self.local_path = os.path.join(project_dir, "data", ".config.yaml")
        self.store = LocalConfigStore(self.local_path)

    def read_public(self):
        default_config = load_default_config(self.default_path)
        with self.store.locked():
            local_config = self.store.read_unlocked()
            config_path = (
                "data/config.d/" if self.store.sections_dir.exists()
                else "data/.config.yaml"
            )
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
            "config_path": config_path,
        }

    def update(self, patch):
        if not isinstance(patch, Mapping):
            raise ValueError("config must be an object")

        unsupported = sorted(set(patch) - EDITABLE_ROOTS)
        if unsupported:
            raise ValueError(f"Unsupported configuration section: {unsupported[0]}")

        with self.store.locked():
            default_config = load_default_config(self.default_path)
            local_config = self.store.read_unlocked()
            current_effective = merge_configs(default_config, local_config)
            safe_patch = _drop_blank_secrets(patch, current_effective)
            updated_local = _merge_editor_patch(local_config, safe_patch)
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

        soundbank_config = config.get("static_soundbank", {})
        if not isinstance(soundbank_config, Mapping):
            raise ValueError("static_soundbank must be an object")
        soundbank_entries = soundbank_config.get("entries", {})
        if not isinstance(soundbank_entries, Mapping):
            raise ValueError("static_soundbank.entries must be an object")

        normalized_phrases = {}
        for phrase in soundbank_entries:
            normalized = normalize_soundbank_text(phrase)
            if not normalized:
                raise ValueError("Soundbank phrase must contain matchable text")
            previous = normalized_phrases.get(normalized)
            if previous is not None:
                raise ValueError(
                    "Soundbank phrases normalize to the same key: "
                    f"{previous!r} and {phrase!r}"
                )
            normalized_phrases[normalized] = phrase

    def _write_atomic(self, config):
        # update() holds the store lock through read, validation and publication.
        self.store.write_unlocked(config)
