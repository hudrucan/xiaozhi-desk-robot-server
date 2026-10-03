"""Lossless, read-only memory inspection for configuration source switching."""

from pathlib import Path

import yaml

from config.config_loader import get_project_dir
from config.config_store import canonical_bytes
from core.memory_schema import EntryNormalization
from core.memory_storage import MemoryReconciliationRequired


def explicit_memory_config(config):
    selected = config.get("selected_module", {}).get("Memory")
    provider = config.get("Memory", {}).get(selected, {})
    return provider if provider.get("type", selected) == "mem_local_explicit" else None


def memory_path(config):
    path = Path(str(config.get("path", "data/.memory.yaml")).strip())
    return path if path.is_absolute() else Path(get_project_dir()) / path


def read_local_memory(provider_config):
    """Use source settings, but reject any repair/truncation/filtering of records."""
    if provider_config is None:
        return None
    try:
        try:
            content = memory_path(provider_config).read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        scopes = yaml.safe_load(content)
        if scopes is None:
            return {}
        if not isinstance(scopes, dict):
            raise ValueError("Invalid memory root")
        normalizer = EntryNormalization()
        normalizer.entry_max_chars = max(50, int(provider_config.get("entry_max_chars", 300)))
        max_entries = max(1, int(provider_config.get("max_entries", 200)))
        for scope, entries in scopes.items():
            if (not isinstance(scope, str) or not scope.strip()
                    or not isinstance(entries, list) or len(entries) > max_entries):
                raise ValueError("Memory scope requires reconciliation")
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("content"), str):
                    raise ValueError("Invalid memory entry")
                normalized = normalizer._normalize_entry(entry)
                # Canonical comparison distinguishes e.g. True from 1. YAML
                # key order/formatting is neutral; changing persisted values is not.
                if not normalized["content"] or canonical_bytes(normalized) != canonical_bytes(entry):
                    raise ValueError("Memory normalization would lose persisted data")
        return scopes
    except (OSError, ValueError, TypeError, yaml.YAMLError):
        raise MemoryReconciliationRequired() from None


def require_memory_match(source, destination, *, allow_empty_source=False):
    if source is None:
        return  # The current source does not select explicit memory.
    source_records = {scope: entries for scope, entries in source.items() if entries}
    if allow_empty_source and not source_records:
        return
    destination_records = {scope: entries for scope, entries in (destination or {}).items() if entries}
    if canonical_bytes(source_records) != canonical_bytes(destination_records):
        raise MemoryReconciliationRequired()
