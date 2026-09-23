import os
import tempfile
import threading
import uuid
from datetime import datetime

import yaml

from config.config_loader import get_project_dir
from ..base import MemoryProviderBase, logger


TAG = __name__
_FILE_LOCK = threading.RLock()


class MemoryProvider(MemoryProviderBase):
    """Local memory that changes only through explicit memory tools."""

    def __init__(self, config, summary_memory=None):
        super().__init__(config)
        configured_path = str(config.get("path", "data/.memory.yaml")).strip()
        self.memory_path = (
            configured_path
            if os.path.isabs(configured_path)
            else os.path.join(get_project_dir(), configured_path)
        )
        self.max_entries = max(1, int(config.get("max_entries", 50)))
        self.entry_max_chars = max(50, int(config.get("entry_max_chars", 300)))
        self.inject_max_chars = max(0, int(config.get("inject_max_chars", 1200)))
        self.entries = []

    def init_memory(self, role_id, llm, summary_memory=None, **kwargs):
        super().init_memory(role_id, llm, **kwargs)
        self.entries = self._load_entries()

    async def save_memory(self, msgs, session_id=None):
        """Conversation shutdown never writes implicit memories."""
        return None

    async def query_memory(self, query: str) -> str:
        if self.inject_max_chars == 0:
            return ""

        rendered = []
        used_chars = 0
        for entry in reversed(self.entries):
            line = f"- {entry['content']}"
            added_chars = len(line) + (1 if rendered else 0)
            if rendered and used_chars + added_chars > self.inject_max_chars:
                break
            if not rendered and len(line) > self.inject_max_chars:
                line = line[: self.inject_max_chars].rstrip()
                added_chars = len(line)
            rendered.append(line)
            used_chars += added_chars

        rendered.reverse()
        return "\n".join(rendered)

    def remember(self, content: str):
        normalized = " ".join(str(content or "").split()).strip()
        if not normalized:
            return False
        normalized = normalized[: self.entry_max_chars].rstrip()

        matching_entry = next(
            (
                entry
                for entry in self.entries
                if entry.get("content", "").casefold() == normalized.casefold()
            ),
            None,
        )
        if matching_entry is not None:
            matching_entry["updated_at"] = self._now()
        else:
            timestamp = self._now()
            self.entries.append(
                {
                    "id": uuid.uuid4().hex,
                    "content": normalized,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                }
            )
            self.entries = self.entries[-self.max_entries :]

        self._save_entries()
        return True

    def forget(self, query: str):
        normalized = " ".join(str(query or "").split()).strip().casefold()
        if not normalized:
            return 0

        kept = []
        removed = 0
        for entry in self.entries:
            content = entry.get("content", "").casefold()
            if normalized in content:
                removed += 1
            else:
                kept.append(entry)

        if removed:
            self.entries = kept
            self._save_entries()
        return removed

    def list_entries(self):
        return [entry.get("content", "") for entry in self.entries if entry.get("content")]

    def _load_entries(self):
        if not self.role_id:
            return []
        with _FILE_LOCK:
            if not os.path.exists(self.memory_path):
                return []
            try:
                with open(self.memory_path, "r", encoding="utf-8") as memory_file:
                    all_memory = yaml.safe_load(memory_file) or {}
            except (OSError, yaml.YAMLError) as error:
                logger.bind(tag=TAG).error(f"Failed to load local memory: {error}")
                return []

        if not isinstance(all_memory, dict):
            return []

        stored = all_memory.get(self.role_id, [])
        if not isinstance(stored, list):
            return []
        return [
            entry
            for entry in stored
            if isinstance(entry, dict) and isinstance(entry.get("content"), str)
        ][-self.max_entries :]

    def _save_entries(self):
        os.makedirs(os.path.dirname(self.memory_path), exist_ok=True)
        with _FILE_LOCK:
            all_memory = {}
            if os.path.exists(self.memory_path):
                try:
                    with open(self.memory_path, "r", encoding="utf-8") as memory_file:
                        all_memory = yaml.safe_load(memory_file) or {}
                except (OSError, yaml.YAMLError) as error:
                    logger.bind(tag=TAG).warning(
                        f"Replacing unreadable local memory file: {error}"
                    )

            if not isinstance(all_memory, dict):
                logger.bind(tag=TAG).warning(
                    "Replacing local memory file with an invalid root value"
                )
                all_memory = {}

            all_memory[self.role_id] = self.entries
            target_dir = os.path.dirname(self.memory_path)
            temp_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    "w",
                    encoding="utf-8",
                    dir=target_dir,
                    prefix=".memory-",
                    suffix=".tmp",
                    delete=False,
                ) as temp_file:
                    temp_path = temp_file.name
                    yaml.safe_dump(all_memory, temp_file, allow_unicode=True)
                    temp_file.flush()
                    os.fsync(temp_file.fileno())
                os.replace(temp_path, self.memory_path)
            finally:
                if temp_path and os.path.exists(temp_path):
                    os.unlink(temp_path)

    @staticmethod
    def _now():
        return datetime.now().astimezone().isoformat(timespec="seconds")
