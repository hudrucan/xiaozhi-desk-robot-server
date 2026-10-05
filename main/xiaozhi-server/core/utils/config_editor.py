import copy
from collections.abc import Mapping
from contextlib import nullcontext

from config.config_loader import (
    merge_configs,
)
from config.config_store import get_config_store
from config.config_validation import validate_config
from core.soundbank import SoundbankError
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
    "cluster",
    "delete_audio",
    "device_mcp_tool_cache",
    "dump_full_llm_request",
    "empty_response",
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
    """Apply the same Settings patch/secret semantics to either desired store."""

    def __init__(self, store=None):
        self.store = store or get_config_store()

    def read_public(self):
        with self.store.locked():
            default_config = self.store.defaults_unlocked()
            local_config = self.store.read_unlocked()
            source = self.store.status_unlocked()
            config_path = source["source"]
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
            "configuration_source": source,
            "restart_required": source["restart_required"],
        }

    def update(self, patch, soundbank_cleanup=None, draft_id=None, retired_drafts=(), base_revision=None):
        if not isinstance(patch, Mapping):
            raise ValueError("config must be an object")

        unsupported = sorted(set(patch) - EDITABLE_ROOTS)
        if unsupported:
            raise ValueError(f"Unsupported configuration section: {unsupported[0]}")

        cleanup_result = None
        with (soundbank_cleanup.lock if soundbank_cleanup else nullcontext()), self.store.locked():
            self.store.prepare_commit_unlocked(base_revision)
            default_config = self.store.defaults_unlocked()
            local_config = self.store.read_unlocked()
            current_effective = merge_configs(default_config, local_config)
            editable = self.store.settings_overrides_unlocked()
            # Cluster edits must not copy a serving node's exceptions (including
            # redacted secrets) into the shared layer. Local behavior is unchanged.
            secret_baseline = self.store.settings_secret_baseline_unlocked()
            safe_patch = _drop_blank_secrets(patch, secret_baseline)
            updated_local = _merge_editor_patch(editable, safe_patch)
            prepared = self.store.prepare_settings_candidate_unlocked(updated_local, patch)
            effective_config = prepared.effective_config
            self._validate(effective_config)
            cleanup_prepared = False
            if soundbank_cleanup and "static_soundbank" in safe_patch:
                try:
                    soundbank_cleanup.prepare_save(
                        current_effective, effective_config, draft_id, retired_drafts
                    )
                    cleanup_prepared = True
                except (OSError, ValueError, SoundbankError) as error:
                    cleanup_result = {"errors": [str(error)]}
            self._write_atomic(prepared, base_revision)
            if cleanup_prepared:
                try:
                    cleanup_result = soundbank_cleanup.after_save(
                        effective_config, draft_id, retired_drafts
                    )
                except (OSError, ValueError, SoundbankError) as error:
                    cleanup_result = {"errors": [str(error)]}

        payload = self.read_public()
        if cleanup_result is not None:
            payload["soundbank_cleanup"] = cleanup_result
        return payload

    def cleanup_soundbank(self, cleanup, scan_unused=False, confirmation=None):
        with cleanup.lock, self.store.locked():
            if scan_unused:
                self.store.refresh_unlocked(strict=True)
            effective = merge_configs(
                self.store.defaults_unlocked(), self.store.read_unlocked()
            )
            if scan_unused:
                return cleanup.unused(effective, confirmation)
            return cleanup.cleanup_pending(effective)

    _validate = staticmethod(validate_config)

    def _write_atomic(self, prepared, base_revision=None):
        # update() holds the store lock through read, validation and publication.
        self.store.commit_prepared_unlocked(prepared, base_revision)
