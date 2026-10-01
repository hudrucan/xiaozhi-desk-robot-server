"""Sectioned private overrides with recoverable multi-file Settings writes."""

import copy
import hashlib
import os
import re
import shutil
import tempfile
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path

import portalocker
import yaml

from config.config_loader import get_project_dir, merge_configs


SECTION_ROOTS = {
    "runtime.yaml": (
        "server", "delete_audio", "close_connection_no_voice_time", "tts_timeout",
        "tool_call_timeout", "asr_min_audio_ms", "asr_audio_queue_max_frames",
        "stop_tts_notify_voice", "enable_websocket_ping", "tts_audio_send_delay",
        "xiaozhi",
    ),
    "assistant.yaml": (
        "enable_direct_answer_tool", "enable_wakeup_words_response_cache",
        "enable_greeting", "wakeup_greeting", "enable_stop_tts_notify",
        "exit_commands", "exit_farewell", "wakeup_words", "prompt",
        "prompt_template", "system_error_response", "tool_error_response",
        "tool_timeout_response", "end_prompt",
    ),
    "diagnostics.yaml": (
        "log", "enable_turn_metrics", "dump_full_llm_request", "llm_request_dump_file",
    ),
    "integrations.yaml": (
        "device_mcp_tool_cache", "mcp_endpoint", "context_providers", "plugins",
        "voiceprint",
    ),
    "soundbank.yaml": ("static_soundbank",),
    "benchmarks.yaml": ("module_test",),
    "providers/selection.yaml": ("selected_module",),
    "providers/vad.yaml": ("VAD",),
    "providers/asr.yaml": ("ASR",),
    "providers/llm.yaml": ("LLM",),
    "providers/vllm.yaml": ("VLLM",),
    "providers/tts.yaml": ("TTS",),
    "providers/memory.yaml": ("Memory",),
    "providers/intent.yaml": ("Intent",),
}
_PROCESS_LOCK = threading.RLock()


def split_local_config(config):
    """Route known settings to sections; retain unknown roots in the legacy file."""
    remaining = copy.deepcopy(dict(config))
    sections = {name: {} for name in SECTION_ROOTS}
    for filename, roots in SECTION_ROOTS.items():
        for root in roots:
            if root in remaining:
                sections[filename][root] = remaining.pop(root)

    server = sections["runtime.yaml"].get("server")
    settings = server.get("settings") if isinstance(server, Mapping) else None
    if isinstance(settings, Mapping) and "diagnostics" in settings:
        diagnostics = settings.pop("diagnostics")
        sections["diagnostics.yaml"]["server"] = {
            "settings": {"diagnostics": diagnostics}
        }
        if not settings:
            server.pop("settings")
        if not server:
            sections["runtime.yaml"].pop("server")
    return remaining, sections


class LocalConfigStore:
    """All readers/writers lock and recover a pending transaction before use."""

    def __init__(self, local_path=None):
        self.local_path = Path(local_path or Path(get_project_dir()) / "data/.config.yaml")
        self.directory = self.local_path.parent.resolve()
        self.sections_dir = self.directory / "config.d"
        self.journal_path = self.directory / ".config.transaction.yaml"
        self.targets = {self.local_path.name} | {
            f"config.d/{name}" for name in SECTION_ROOTS
        }

    @contextmanager
    def locked(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        with _PROCESS_LOCK, portalocker.Lock(
            str(self.directory / ".config.lock"), mode="a", timeout=5
        ):
            self._recover()
            yield self

    def read_unlocked(self):
        """Read under locked(); section files override remaining legacy values."""
        local_path = self._target_path(self.local_path.name)
        config = self._read_object(local_path) if local_path.exists() else {}
        if self.sections_dir.exists():
            known = {self.sections_dir / name for name in SECTION_ROOTS}
            if any(path not in known for path in self.sections_dir.rglob("*.yaml")):
                raise ValueError("Unknown local config section; put extra roots in data/.config.yaml")
        for filename in SECTION_ROOTS:
            path = self._target_path(f"config.d/{filename}")
            if not path.exists():
                continue
            fragment = self._read_object(path)
            remaining, routed = split_local_config(fragment)
            if remaining or any(value for name, value in routed.items() if name != filename):
                raise ValueError(f"Settings stored in the wrong local section: {filename}")
            config = merge_configs(config, fragment)
        return config

    @staticmethod
    def _read_object(path):
        with path.open("r", encoding="utf-8") as file:
            value = yaml.safe_load(file)
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise ValueError(f"Local configuration must be an object: {path.name}")
        return value

    @staticmethod
    def _sync_directory(path):
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _write_bytes(path, content):
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())

    def _atomic_bytes(self, path, content):
        descriptor, name = tempfile.mkstemp(prefix=".config.", dir=self.directory)
        temporary_path = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, path)
            self._sync_directory(self.directory)
        finally:
            temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _yaml_bytes(value):
        return yaml.safe_dump(
            value, allow_unicode=True, default_flow_style=False, sort_keys=False
        ).encode("utf-8")

    def write_unlocked(self, config):
        """Publish a durable commit intent, then replace changed section files."""
        previous = self.read_unlocked()
        remaining, sections = split_local_config(config)
        desired = {self.local_path.name: remaining}
        desired.update({f"config.d/{name}": value for name, value in sections.items()})
        changes = {
            name: value for name, value in desired.items()
            if not (self.directory / name).exists()
            or self._read_object(self.directory / name) != value
        }
        if not changes:
            return

        migration_backup = self.directory / f"{self.local_path.name}.pre-split.backup"
        if self.local_path.exists() and not migration_backup.exists():
            self._write_bytes(migration_backup, self.local_path.read_bytes())
            self._sync_directory(self.directory)
        self._atomic_bytes(
            self.directory / f"{self.local_path.name}.backup", self._yaml_bytes(previous)
        )

        stage = Path(tempfile.mkdtemp(prefix=".config-stage-", dir=self.directory))
        published = False
        try:
            entries = []
            for index, (name, value) in enumerate(changes.items()):
                # Check every destination before publishing the transaction.
                self._target_path(name)
                staged = f"{index}.yaml"
                content = self._yaml_bytes(value)
                self._write_bytes(stage / staged, content)
                entries.append({
                    "target": name, "staged": staged,
                    "sha256": hashlib.sha256(content).hexdigest(),
                })
            self._sync_directory(stage)
            self._atomic_bytes(self.journal_path, self._yaml_bytes({
                "stage": stage.name, "files": entries,
            }))
            published = True
            self._recover()
        finally:
            # A committed journal owns its stage until recovery completes.
            if not published and not self.journal_path.exists():
                shutil.rmtree(stage)

    def _target_path(self, name):
        if name not in self.targets:
            raise ValueError("Invalid local configuration transaction target")
        path = self.directory / name
        if not path.resolve().is_relative_to(self.directory):
            raise ValueError("Local configuration transaction must stay within data/")
        return path

    def _recover(self):
        if not self.journal_path.exists():
            return
        journal = self._read_object(self.journal_path)
        stage_name = journal.get("stage")
        entries = journal.get("files")
        if (
            not isinstance(stage_name, str)
            or not re.fullmatch(r"\.config-stage-[\w-]+", stage_name)
            or not isinstance(entries, list) or not entries
        ):
            raise ValueError("Invalid local configuration transaction journal")
        stage = self.directory / stage_name
        if stage.is_symlink() or not stage.is_dir():
            raise ValueError("Missing local configuration transaction stage")
        validated = []
        targets, staged_names = set(), set()
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise ValueError("Invalid local configuration transaction entry")
            name, staged = entry.get("target"), entry.get("staged")
            digest = entry.get("sha256")
            if (
                not isinstance(name, str) or not isinstance(staged, str)
                or not re.fullmatch(r"\d+\.yaml", staged)
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or name in targets or staged in staged_names
            ):
                raise ValueError("Invalid local configuration transaction entry")
            target = self._target_path(name)
            source = stage / staged
            if source.is_symlink() or not (source.is_file() or target.is_file()):
                raise ValueError("Missing staged local configuration file")
            content_path = source if source.is_file() else target
            if hashlib.sha256(content_path.read_bytes()).hexdigest() != digest:
                raise ValueError("Local configuration transaction file checksum mismatch")
            targets.add(name)
            staged_names.add(staged)
            validated.append((source, target))
        for source, target in validated:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            # Persist newly-created section directories before removing staged
            # names, so a power loss cannot strand an already-renamed file.
            if target.parent != self.directory:
                self._sync_directory(target.parent)
                self._sync_directory(self.sections_dir)
                self._sync_directory(self.directory)
            if source.exists():
                os.replace(source, target)
            # Also sync an already-renamed target when recovering after a crash.
            self._sync_directory(target.parent)
            self._sync_directory(stage)
        if self.sections_dir.exists():
            self._sync_directory(self.sections_dir)
        self._sync_directory(self.directory)
        self.journal_path.unlink()
        self._sync_directory(self.directory)
        shutil.rmtree(stage)


def load_local_config(local_path=None):
    store = LocalConfigStore(local_path)
    with store.locked():
        return store.read_unlocked()
