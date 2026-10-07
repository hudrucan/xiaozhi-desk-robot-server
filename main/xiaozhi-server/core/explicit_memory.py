import copy
import logging
import os
import re
import tempfile
import threading
import uuid
from difflib import SequenceMatcher

import yaml

from config.config_loader import get_project_dir
from core.memory_schema import EntryNormalization, _MEMORY_TYPES


TAG = __name__
_FILE_LOCK = threading.RLock()


class ExplicitMemory(EntryNormalization):
    """Explicit deterministic memory with Local YAML or a synced Cloud backend."""

    def __init__(self, config, summary_memory=None, *, logger=None):
        self.config, self.role_id, self.llm = config, None, None
        self.logger = logger or logging.getLogger(__name__)
        configured_path = str(config.get("path", "data/.memory.yaml")).strip()
        self.memory_path = (
            configured_path
            if os.path.isabs(configured_path)
            else os.path.join(get_project_dir(), configured_path)
        )
        self.max_entries = max(1, int(config.get("max_entries", 200)))
        self.entry_max_chars = max(50, int(config.get("entry_max_chars", 300)))
        legacy_inject_max = max(0, int(config.get("inject_max_chars", 1200)))
        self.pinned_max_chars = max(
            0, int(config.get("pinned_max_chars", legacy_inject_max))
        )
        self.recall_enabled = self._as_bool(config.get("recall_enabled", True))
        self.recall_max_chars = max(
            0, int(config.get("recall_max_chars", legacy_inject_max))
        )
        self.recall_top_k = max(1, int(config.get("recall_top_k", 6)))
        self.recall_min_score = max(
            0.0, float(config.get("recall_min_score", 2.5))
        )
        self.recall_recent_turns = min(
            3, max(2, int(config.get("recall_recent_turns", 3)))
        )
        self.recall_fuzzy_threshold = min(
            1.0, max(0.75, float(config.get("recall_fuzzy_threshold", 0.84)))
        )
        configured_stop_words = config.get("recall_stop_words", [])
        if not isinstance(configured_stop_words, list):
            configured_stop_words = []
        self.recall_stop_words = {
            str(word).strip().casefold()
            for word in configured_stop_words
            if str(word).strip()
        }
        configured_aliases = config.get("recall_aliases", {})
        if not isinstance(configured_aliases, dict):
            configured_aliases = {}
        self.recall_aliases = {
            str(alias).strip().casefold(): str(canonical).strip().casefold()
            for alias, canonical in configured_aliases.items()
            if str(alias).strip() and str(canonical).strip()
        }
        self._entries_lock = threading.RLock()
        self.entries = []
        self.scope_source = None
        self.storage = None
        self.memory_revision = None

    def set_llm(self, llm):
        self.llm = llm

    def init_memory(self, role_id, llm, summary_memory=None, **kwargs):
        with self._entries_lock:
            self.role_id, self.llm = role_id, llm
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

    async def query_memory(self, query: str, context=None) -> str:
        recalled = self.recall(query, context=context)
        self.logger.debug(
            f"Automatic local memory recall: {'hit' if recalled else 'miss'}"
        )
        return recalled

    def resolve_active_project(self, query: str, recent_messages=None, current=None):
        """Resolve a project mention without storing session state in the provider."""
        with self._entries_lock:
            projects = {
                entry.get("project")
                for entry in self.entries
                if entry.get("active") and entry.get("project")
            }
        if not projects:
            return None

        messages = [query]
        recent = [str(message or "") for message in (recent_messages or [])]
        messages.extend(reversed(recent[-self.recall_recent_turns * 2 :]))
        ordered_projects = sorted(projects, key=lambda value: (-len(value), value))
        for message in messages:
            normalized_message = self._normalize_for_search(message)
            for project in ordered_projects:
                normalized_project = self._normalize_for_search(project)
                if self._contains_phrase(normalized_message, normalized_project):
                    return project
        return current if current in projects else None

    def recall(self, query: str, context=None):
        context = context if isinstance(context, dict) else {}
        active_project = self._normalize_optional_text(context.get("active_project"))
        recent_messages = [
            str(message or "")
            for message in context.get("recent_messages", [])
            if str(message or "").strip()
        ]

        with self._entries_lock:
            entries = copy.deepcopy(self.entries)

        superseded_ids = {
            entry.get("supersedes")
            for entry in entries
            if entry.get("supersedes")
        }
        eligible = [
            entry
            for entry in entries
            if entry.get("active") and entry.get("id") not in superseded_ids
        ]

        pinned = [
            entry
            for entry in eligible
            if entry.get("pinned")
            and (
                not entry.get("project")
                or (
                    active_project
                    and self._same_text(entry.get("project"), active_project)
                )
            )
        ]
        pinned.sort(
            key=lambda entry: (
                bool(active_project and self._same_text(entry.get("project"), active_project)),
                int(entry.get("importance", 3)),
                str(entry.get("updated_at", "")),
                str(entry.get("id", "")),
            ),
            reverse=True,
        )

        recalled = []
        if self.recall_enabled and self.recall_max_chars > 0:
            current_text = self._normalize_for_search(query)
            combined_text = self._normalize_for_search(
                " ".join([*recent_messages, str(query or "")])
            )
            query_terms = self._search_terms(combined_text)
            if query_terms:
                ranked = []
                total = max(1, len(eligible))
                for index, entry in enumerate(eligible):
                    if entry.get("pinned"):
                        continue
                    score, rank_key = self._score_entry(
                        entry,
                        current_text=current_text,
                        combined_text=combined_text,
                        query_terms=query_terms,
                        active_project=active_project,
                        recency=(index + 1) / total,
                    )
                    if score >= self.recall_min_score:
                        ranked.append((rank_key, score, entry))
                ranked.sort(
                    key=lambda item: (
                        *item[0],
                        item[1],
                        str(item[2].get("updated_at", "")),
                        str(item[2].get("id", "")),
                    ),
                    reverse=True,
                )
                recalled = [entry for _, _, entry in ranked[: self.recall_top_k]]

        pinned_ids = {entry.get("id") for entry in pinned}
        pinned_content = {
            self._normalize_for_search(entry.get("content")) for entry in pinned
        }
        recalled = [
            entry
            for entry in recalled
            if entry.get("id") not in pinned_ids
            and self._normalize_for_search(entry.get("content")) not in pinned_content
        ]

        sections = []
        pinned_heading = "Pinned memories:"
        rendered_pinned = self._render_entries(
            pinned, max(0, self.pinned_max_chars - len(pinned_heading) - 1)
        )
        if rendered_pinned:
            sections.append(pinned_heading + "\n" + rendered_pinned)
        recall_heading = "Relevant memories:"
        rendered_recalled = self._render_entries(
            recalled, max(0, self.recall_max_chars - len(recall_heading) - 1)
        )
        if rendered_recalled:
            sections.append(recall_heading + "\n" + rendered_recalled)
        return "\n\n".join(sections)

    def _score_entry(
        self,
        entry,
        current_text,
        combined_text,
        query_terms,
        active_project,
        recency,
    ):
        content = self._normalize_for_search(entry.get("content"))
        entities = [
            self._normalize_for_search(value) for value in entry.get("entities", [])
        ]
        tags = [self._normalize_for_search(value) for value in entry.get("tags", [])]
        project = self._normalize_for_search(entry.get("project"))
        content_terms = self._search_terms(content)
        candidate_terms = content_terms | {
            term for value in [*entities, *tags, project] for term in value.split()
        }
        entity_matches = sum(
            1 for value in entities if self._contains_phrase(combined_text, value)
        )
        tag_matches = sum(
            1 for value in tags if self._contains_phrase(combined_text, value)
        )
        project_match = bool(
            active_project and project == self._normalize_for_search(active_project)
        )
        phrase_match = bool(
            current_text
            and content
            and (
                self._contains_phrase(content, current_text)
                or self._contains_phrase(current_text, content)
            )
        )
        overlap = len(query_terms & candidate_terms)
        fuzzy_matches = 0
        if not overlap:
            for query_term in query_terms:
                if len(query_term) < 4:
                    continue
                if any(
                    len(candidate) >= 4
                    and SequenceMatcher(None, query_term, candidate).ratio()
                    >= self.recall_fuzzy_threshold
                    for candidate in candidate_terms
                ):
                    fuzzy_matches += 1

        signal_score = (
            min(entity_matches, 2) * 8.0
            + min(tag_matches, 2) * 5.0
            + (4.0 if project_match else 0.0)
            + (4.0 if phrase_match else 0.0)
            + min(overlap * 1.5, 4.5)
            + min(fuzzy_matches * 0.75, 2.0)
        )
        if signal_score == 0:
            return 0.0, ()
        importance_score = int(entry.get("importance", 3)) * 0.25
        recency_score = max(0.0, min(1.0, recency)) * 0.5
        rank_key = (
            bool(entity_matches),
            entity_matches,
            bool(tag_matches),
            tag_matches,
            project_match,
            phrase_match,
            overlap,
            fuzzy_matches,
            int(entry.get("importance", 3)),
            recency,
        )
        return signal_score + importance_score + recency_score, rank_key

    def bind_storage(self, storage):
        """Local binds None; Cloud runtime preparation supplies a synced backend."""
        with self._entries_lock:
            self.storage = storage
            if storage is not None:
                self.memory_revision = storage.status()["memory_revision"]
                self.entries = self._load_entries()

    def sync_memory(self):
        with self._entries_lock:
            if self.storage is not None:
                self.storage.sync()
                self.memory_revision = self.storage.status()["memory_revision"]
                self.entries = self._load_entries()

    def _mutate_entries(self, transform):
        with self._entries_lock:
            if self.storage is not None:
                result, entries = self.storage.mutate(
                    self.role_id, self.memory_revision, transform
                )
                self.entries = entries[-self.max_entries :]
                self.memory_revision = self.storage.status()["memory_revision"]
                return result
            # Keep Local's YAML save and externally observable failure behavior.
            candidate = copy.deepcopy(self.entries)
            result = transform(candidate)
            if result:
                self.entries = candidate
                self._save_entries()
            return result

    def remember(self, content: str, **metadata):
        normalized = self._normalize_content(content)
        if not normalized:
            return False
        normalized_metadata = self._normalize_metadata(metadata)

        def transform(entries):
            supersedes = normalized_metadata.get("supersedes")
            if supersedes:
                old_entry = self._find_entry(supersedes, entries)
                if old_entry is None:
                    raise ValueError("Superseded memory was not found")
                old_entry["active"] = False
                old_entry["updated_at"] = self._now()
            matching_entry = next(
                (entry for entry in entries
                 if not supersedes
                 and entry.get("content", "").casefold() == normalized.casefold()
                 and self._same_text(entry.get("project"), normalized_metadata.get("project"))),
                None,
            )
            if matching_entry is not None:
                matching_entry.update(normalized_metadata)
                matching_entry["updated_at"] = self._now()
                entries.remove(matching_entry)
                entries.append(matching_entry)
            else:
                timestamp = self._now()
                entries.append(self._normalize_entry({
                    "id": uuid.uuid4().hex, "content": normalized, **normalized_metadata,
                    "created_at": timestamp, "updated_at": timestamp,
                }))
                entries[:] = entries[-self.max_entries :]
            return True

        return self._mutate_entries(transform)

    def forget(self, query: str):
        normalized = self._normalize_for_search(query)
        if not normalized:
            return 0

        def transform(entries):
            changed = 0
            for entry in entries:
                if entry.get("active") and normalized in self._normalize_for_search(entry.get("content")):
                    entry["active"] = False
                    entry["updated_at"] = self._now()
                    changed += 1
            return changed

        return self._mutate_entries(transform)

    def list_entries(self):
        with self._entries_lock:
            return [
                self._render_entry(entry)
                for entry in reversed(self.entries)
                if entry.get("active") and entry.get("content")
            ]

    def inspect_entries(self):
        """Return editable memory state for the local settings UI."""
        with self._entries_lock:
            return {
                **(self.storage.status() if self.storage is not None else {"storage_source": "local"}),
                "initialized": bool(self.role_id),
                "device_id": self.role_id,
                "scope_source": self.scope_source,
                "recall_enabled": self.recall_enabled,
                "max_entries": self.max_entries,
                "entry_max_chars": self.entry_max_chars,
                "memory_types": sorted(_MEMORY_TYPES),
                "entries": copy.deepcopy(list(reversed(self.entries))),
            }

    def update_entry(self, entry_id: str, content: str, **metadata):
        normalized = self._normalize_content(content)
        if not normalized:
            raise ValueError("Memory content cannot be empty")
        normalized_metadata = self._normalize_metadata(metadata)

        def transform(entries):
            entry = self._find_entry(entry_id, entries)
            if entry is None:
                return False
            supersedes = normalized_metadata.get("supersedes")
            if supersedes and supersedes == entry.get("id"):
                raise ValueError("A memory cannot supersede itself")
            if supersedes:
                old_entry = self._find_entry(supersedes, entries)
                if old_entry is None:
                    raise ValueError("Superseded memory was not found")
                old_entry["active"] = False
                old_entry["updated_at"] = self._now()
            entry["content"] = normalized
            entry.update(normalized_metadata)
            entry["updated_at"] = self._now()
            entries.remove(entry)
            entries.append(entry)
            return True

        return self._mutate_entries(transform)

    def delete_entry(self, entry_id: str):
        def transform(entries):
            kept = [entry for entry in entries if entry.get("id") != str(entry_id)]
            if len(kept) == len(entries):
                return False
            entries[:] = kept
            return True

        return self._mutate_entries(transform)

    def _normalize_for_search(self, value):
        terms = re.findall(r"\w+", str(value or "").casefold(), flags=re.UNICODE)
        expanded = []
        for term in terms:
            alias = self.recall_aliases.get(term, term)
            expanded.extend(re.findall(r"\w+", alias, flags=re.UNICODE))
        return " ".join(expanded)

    def _search_terms(self, value):
        return {term for term in value.split() if term not in self.recall_stop_words}

    @staticmethod
    def _contains_phrase(text, phrase):
        if not text or not phrase:
            return False
        return f" {phrase} " in f" {text} "

    def _same_text(self, left, right):
        return self._normalize_for_search(left) == self._normalize_for_search(right)

    def _render_entry(self, entry):
        metadata = [f"id={entry.get('id')}", f"type={entry.get('type', 'fact')}"]
        if entry.get("project"):
            metadata.append(f"project={entry['project']}")
        return f"[{'; '.join(metadata)}] {entry.get('content', '')}"

    def _render_entries(self, entries, max_chars):
        if max_chars == 0:
            return ""
        rendered = []
        used_chars = 0
        for entry in entries:
            line = "- " + self._render_entry(entry)
            added_chars = len(line) + (1 if rendered else 0)
            if rendered and used_chars + added_chars > max_chars:
                self.logger.warning(
                    "Memory injection truncated by character budget"
                )
                break
            if not rendered and len(line) > max_chars:
                line = line[:max_chars].rstrip()
                added_chars = len(line)
            rendered.append(line)
            used_chars += added_chars
        return "\n".join(rendered)

    def _find_entry(self, entry_id, entries=None):
        return next(
            (entry for entry in (self.entries if entries is None else entries)
             if entry.get("id") == str(entry_id)),
            None,
        )

    def _load_entries(self):
        if not self.role_id:
            return []
        return self._entries_for_role(self._read_memory_file(), self.role_id)

    def _read_memory_file(self):
        if self.storage is not None:
            validated = self.storage.scopes()
            if not getattr(self.storage, "materialize", True):
                return validated
            try:
                with open(self.memory_path, "r", encoding="utf-8") as memory_file:
                    materialized = yaml.safe_load(memory_file)
                if materialized == validated:
                    return materialized
            except (OSError, yaml.YAMLError):
                pass
            # After a successful remote CAS, a failed YAML write cannot hide the
            # committed state from reconnects. Arbitrary YAML is never authority.
            return validated
        with _FILE_LOCK:
            if not os.path.exists(self.memory_path):
                return {}
            try:
                with open(self.memory_path, "r", encoding="utf-8") as memory_file:
                    all_memory = yaml.safe_load(memory_file) or {}
            except (OSError, yaml.YAMLError) as error:
                self.logger.error(f"Failed to load local memory: {error}")
                return {}
        return all_memory if isinstance(all_memory, dict) else {}

    def _entries_for_role(self, all_memory, role_id):
        stored = all_memory.get(role_id, [])
        if not isinstance(stored, list):
            return []
        entries = []
        for entry in stored:
            if not isinstance(entry, dict) or not isinstance(entry.get("content"), str):
                continue
            try:
                normalized = self._normalize_entry(entry)
            except ValueError as error:
                self.logger.warning(f"Skipping invalid local memory: {error}")
                continue
            if normalized["content"]:
                entries.append(normalized)
        return entries[-self.max_entries :]

    def _save_entries(self):
        os.makedirs(os.path.dirname(self.memory_path), exist_ok=True)
        with _FILE_LOCK:
            all_memory = {}
            if os.path.exists(self.memory_path):
                try:
                    with open(self.memory_path, "r", encoding="utf-8") as memory_file:
                        all_memory = yaml.safe_load(memory_file) or {}
                except (OSError, yaml.YAMLError) as error:
                    self.logger.warning(
                        f"Replacing unreadable local memory file: {error}"
                    )
            if not isinstance(all_memory, dict):
                self.logger.warning(
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
                    yaml.safe_dump(
                        all_memory, temp_file, allow_unicode=True, sort_keys=False
                    )
                    temp_file.flush()
                    os.fsync(temp_file.fileno())
                os.replace(temp_path, self.memory_path)
            finally:
                if temp_path and os.path.exists(temp_path):
                    os.unlink(temp_path)
