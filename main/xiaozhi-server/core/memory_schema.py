"""Pure explicit-memory normalization shared by Local and Cloud validation.

No provider/logging imports: runtime preparation runs before modules exist.
"""

import uuid
from datetime import datetime

_MEMORY_TYPES = {
    "fact",
    "preference",
    "decision",
    "project_state",
    "hardware",
    "todo",
    "session",
}
_METADATA_FIELDS = {
    "type",
    "project",
    "entities",
    "tags",
    "importance",
    "pinned",
    "active",
    "supersedes",
}


class EntryNormalization:
    def _normalize_entry(self, entry):
        timestamp = self._now()
        return {
            "id": str(entry.get("id") or uuid.uuid4().hex),
            "content": self._normalize_content(entry.get("content")),
            "type": self._normalize_type(entry.get("type", "fact")),
            "project": self._normalize_optional_text(entry.get("project")),
            "entities": self._normalize_string_list(entry.get("entities", [])),
            "tags": self._normalize_string_list(entry.get("tags", [])),
            "importance": self._normalize_importance(entry.get("importance", 3)),
            "pinned": self._as_bool(entry.get("pinned", False)),
            "active": self._as_bool(entry.get("active", True)),
            "supersedes": self._normalize_optional_text(entry.get("supersedes")),
            "created_at": str(entry.get("created_at") or timestamp),
            "updated_at": str(
                entry.get("updated_at") or entry.get("created_at") or timestamp
            ),
        }

    def _normalize_metadata(self, metadata):
        provided = {key: value for key, value in metadata.items() if key in _METADATA_FIELDS}
        normalized = {}
        if "type" in provided:
            normalized["type"] = self._normalize_type(provided["type"])
        if "project" in provided:
            normalized["project"] = self._normalize_optional_text(provided["project"])
        if "entities" in provided:
            normalized["entities"] = self._normalize_string_list(provided["entities"])
        if "tags" in provided:
            normalized["tags"] = self._normalize_string_list(provided["tags"])
        if "importance" in provided:
            normalized["importance"] = self._normalize_importance(provided["importance"])
        if "pinned" in provided:
            normalized["pinned"] = self._as_bool(provided["pinned"])
        if "active" in provided:
            normalized["active"] = self._as_bool(provided["active"])
        if "supersedes" in provided:
            normalized["supersedes"] = self._normalize_optional_text(provided["supersedes"])
        return normalized

    def _normalize_content(self, content):
        normalized = " ".join(str(content or "").split()).strip()
        return normalized[: self.entry_max_chars].rstrip()

    @staticmethod
    def _normalize_optional_text(value):
        normalized = " ".join(str(value or "").split()).strip()
        return normalized or None

    @staticmethod
    def _normalize_string_list(value):
        if isinstance(value, str):
            value = value.split(",")
        if not isinstance(value, (list, tuple, set)):
            return []
        result = []
        seen = set()
        for item in value:
            normalized = " ".join(str(item or "").split()).strip()
            key = normalized.casefold()
            if normalized and key not in seen:
                seen.add(key)
                result.append(normalized)
        return result

    @staticmethod
    def _normalize_importance(value):
        try:
            return min(5, max(1, int(value)))
        except (TypeError, ValueError):
            return 3

    @staticmethod
    def _normalize_type(value):
        normalized = str(value or "fact").strip().casefold()
        if normalized not in _MEMORY_TYPES:
            raise ValueError(f"Unsupported memory type: {normalized}")
        return normalized

    @staticmethod
    def _as_bool(value):
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "no", "off", ""}
        return bool(value)

    @staticmethod
    def _now():
        return datetime.now().astimezone().isoformat(timespec="seconds")
