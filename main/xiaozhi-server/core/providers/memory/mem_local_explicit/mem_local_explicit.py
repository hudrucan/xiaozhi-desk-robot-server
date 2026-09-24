import copy
import os
import re
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
        self.max_entries = max(1, int(config.get("max_entries", 200)))
        self.entry_max_chars = max(50, int(config.get("entry_max_chars", 300)))
        self.inject_max_chars = max(0, int(config.get("inject_max_chars", 1200)))
        self.recall_enabled = self._as_bool(config.get("recall_enabled", True))
        self.recall_max_chars = max(
            0, int(config.get("recall_max_chars", self.inject_max_chars))
        )
        configured_stop_words = config.get("recall_stop_words", [])
        if not isinstance(configured_stop_words, list):
            configured_stop_words = []
        self.recall_stop_words = {
            str(word).strip().casefold()
            for word in configured_stop_words
            if str(word).strip()
        }
        self._entries_lock = threading.RLock()
        self.entries = []
        self.scope_source = None

    def init_memory(self, role_id, llm, summary_memory=None, **kwargs):
        with self._entries_lock:
            super().init_memory(role_id, llm, **kwargs)
            self.scope_source = "connection"
            self.entries = self._load_entries()

    def select_stored_scope(self):
        """Select the only stored device scope for offline administration."""
        with self._entries_lock:
            if self.role_id:
                return self.role_id

            all_memory = self._read_memory_file()
            stored_roles = [
                role_id
                for role_id, entries in all_memory.items()
                if isinstance(role_id, str) and isinstance(entries, list)
            ]
            if len(stored_roles) != 1:
                return None

            self.role_id = stored_roles[0]
            self.scope_source = "storage"
            self.entries = self._entries_for_role(all_memory, self.role_id)
            return self.role_id

    async def save_memory(self, msgs, session_id=None):
        """Conversation shutdown never writes implicit memories."""
        return None

    async def query_memory(self, query: str) -> str:
        if not self.recall_enabled:
            return ""

        recalled = self.recall(query)
        logger.bind(tag=TAG).debug(
            f"Automatic local memory recall: {'hit' if recalled else 'miss'}"
        )
        return recalled

    def recall(self, query: str):
        if not self.recall_enabled or self.recall_max_chars == 0:
            return ""

        with self._entries_lock:
            candidates = copy.deepcopy(list(reversed(self.entries)))
        normalized_query = self._normalize_for_search(query)
        query_terms = self._search_terms(normalized_query)
        if not query_terms:
            return ""

        ranked = []
        for recency, entry in enumerate(candidates):
            normalized_content = self._normalize_for_search(
                entry.get("content", "")
            )
            content_terms = set(
                re.findall(r"\w+", normalized_content, flags=re.UNICODE)
            )
            overlap = len(query_terms & content_terms)
            phrase_match = bool(
                normalized_query and normalized_query in normalized_content
            )
            if overlap or phrase_match:
                ranked.append((phrase_match, overlap, -recency, entry))

        if not ranked:
            return ""

        ranked.sort(reverse=True, key=lambda item: item[:3])
        return self._render_entries(
            (item[3] for item in ranked), self.recall_max_chars
        )

    @staticmethod
    def _render_entries(entries, max_chars):
        if max_chars == 0:
            return ""

        rendered = []
        used_chars = 0
        for entry in entries:
            line = f"- {entry['content']}"
            added_chars = len(line) + (1 if rendered else 0)
            if rendered and used_chars + added_chars > max_chars:
                break
            if not rendered and len(line) > max_chars:
                line = line[:max_chars].rstrip()
                added_chars = len(line)
            rendered.append(line)
            used_chars += added_chars

        return "\n".join(rendered)

    @staticmethod
    def _normalize_for_search(value):
        return " ".join(str(value or "").casefold().split())

    def _search_terms(self, value):
        return {
            term
            for term in re.findall(r"\w+", value, flags=re.UNICODE)
            if term not in self.recall_stop_words
        }

    @staticmethod
    def _as_bool(value):
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "no", "off"}
        return bool(value)

    def remember(self, content: str):
        normalized = self._normalize_content(content)
        if not normalized:
            return False

        with self._entries_lock:
            matching_entry = next(
                (
                    entry
                    for entry in self.entries
                    if entry.get("content", "").casefold()
                    == normalized.casefold()
                ),
                None,
            )
            if matching_entry is not None:
                matching_entry["updated_at"] = self._now()
                self.entries.remove(matching_entry)
                self.entries.append(matching_entry)
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

        with self._entries_lock:
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
        with self._entries_lock:
            return [
                entry.get("content", "")
                for entry in self.entries
                if entry.get("content")
            ]

    def inspect_entries(self):
        """Return editable memory state for the local settings UI."""
        with self._entries_lock:
            return {
                "initialized": bool(self.role_id),
                "device_id": self.role_id,
                "scope_source": self.scope_source,
                "recall_enabled": self.recall_enabled,
                "max_entries": self.max_entries,
                "entry_max_chars": self.entry_max_chars,
                "entries": copy.deepcopy(list(reversed(self.entries))),
            }

    def update_entry(self, entry_id: str, content: str):
        normalized = self._normalize_content(content)
        if not normalized:
            raise ValueError("Memory content cannot be empty")

        with self._entries_lock:
            entry = next(
                (
                    item
                    for item in self.entries
                    if item.get("id") == str(entry_id)
                ),
                None,
            )
            if entry is None:
                return False
            entry["content"] = normalized
            entry["updated_at"] = self._now()
            self.entries.remove(entry)
            self.entries.append(entry)
            self._save_entries()
        return True

    def delete_entry(self, entry_id: str):
        with self._entries_lock:
            kept = [
                entry
                for entry in self.entries
                if entry.get("id") != str(entry_id)
            ]
            if len(kept) == len(self.entries):
                return False
            self.entries = kept
            self._save_entries()
        return True

    def _normalize_content(self, content):
        normalized = " ".join(str(content or "").split()).strip()
        return normalized[: self.entry_max_chars].rstrip()

    def _load_entries(self):
        if not self.role_id:
            return []
        return self._entries_for_role(self._read_memory_file(), self.role_id)

    def _read_memory_file(self):
        with _FILE_LOCK:
            if not os.path.exists(self.memory_path):
                return {}
            try:
                with open(self.memory_path, "r", encoding="utf-8") as memory_file:
                    all_memory = yaml.safe_load(memory_file) or {}
            except (OSError, yaml.YAMLError) as error:
                logger.bind(tag=TAG).error(f"Failed to load local memory: {error}")
                return {}

        if not isinstance(all_memory, dict):
            return {}
        return all_memory

    def _entries_for_role(self, all_memory, role_id):
        stored = all_memory.get(role_id, [])
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
