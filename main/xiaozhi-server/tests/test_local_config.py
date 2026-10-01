"""Private configuration persistence and interrupted-save regression checks."""

import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from config.local_config import LocalConfigStore


class LocalConfigStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / ".config.yaml"
        self.original = {
            "server": {
                "port": 8000,
                "settings": {"diagnostics": {"thresholds_ms": {"total": 30000}}},
            },
            "selected_module": {"TTS": "MatchaTTS"},
            "TTS": {"MatchaTTS": {"seed": None, "volume_gain": 1.0}},
            "LLM": {"Local": {"api_key": "private-test-key"}},
            "context_providers": [{"headers": {"Authorization": "private-test-header"}}],
            "static_soundbank": {"entries": {"Hello": {"file": "hello.wav"}}},
            "future_setting": {"enabled": False},
        }
        self.original_bytes = ("# Private overrides\n" + yaml.safe_dump(self.original)).encode()
        self.path.write_bytes(self.original_bytes)
        self.store = LocalConfigStore(self.path)

    def migrate(self):
        with self.store.locked():
            self.store.write_unlocked(self.store.read_unlocked())

    def read(self):
        with self.store.locked():
            return self.store.read_unlocked()

    def test_legacy_reads_do_not_migrate(self):
        self.assertEqual(self.read(), self.original)
        self.assertFalse(self.store.sections_dir.exists())
        self.assertEqual(self.path.read_bytes(), self.original_bytes)

    def test_migration_preserves_unknown_roots_secrets_and_original_yaml(self):
        self.migrate()
        self.assertEqual(self.read(), self.original)
        self.assertEqual(yaml.safe_load(self.path.read_text()), {
            "future_setting": {"enabled": False},
        })
        self.assertEqual(
            self.path.with_name(".config.yaml.pre-split.backup").read_bytes(),
            self.original_bytes,
        )
        tts_file = self.store.sections_dir / "providers/tts.yaml"
        self.assertEqual(tts_file.stat().st_mode & 0o777, 0o600)

    def test_save_leaves_untouched_provider_file_and_backs_up_previous_config(self):
        self.migrate()
        tts_file = self.store.sections_dir / "providers/tts.yaml"
        previous_stat = tts_file.stat()
        updated = copy.deepcopy(self.original)
        updated["server"]["port"] = 8001
        updated["static_soundbank"]["entries"] = {}
        with self.store.locked():
            self.store.write_unlocked(updated)
        self.assertEqual(self.read(), updated)
        self.assertEqual(tts_file.stat().st_mtime_ns, previous_stat.st_mtime_ns)
        self.assertEqual(
            yaml.safe_load(self.path.with_name(".config.yaml.backup").read_text()),
            self.original,
        )

    def test_interrupted_multi_section_save_finishes_before_next_read(self):
        self.migrate()
        updated = copy.deepcopy(self.original)
        updated["server"]["port"] = 8002
        updated["TTS"]["MatchaTTS"]["volume_gain"] = 0.9
        replace = os.replace
        count = 0

        def interrupt_second_section(source, target):
            nonlocal count
            if Path(source).parent.name.startswith(".config-stage-"):
                count += 1
                if count == 2:
                    raise OSError("Simulated interrupted save")
            return replace(source, target)

        with self.store.locked(), patch(
            "config.local_config.os.replace", side_effect=interrupt_second_section
        ):
            with self.assertRaises(OSError):
                self.store.write_unlocked(updated)
        self.assertTrue(self.store.journal_path.exists())
        self.assertEqual(self.read(), updated)
        self.assertFalse(self.store.journal_path.exists())

    def test_wrong_section_is_rejected(self):
        self.migrate()
        (self.store.sections_dir / "providers/tts.yaml").write_text(
            "LLM:\n  Local:\n    api_key: private-test-key\n"
        )
        with self.assertRaisesRegex(ValueError, "wrong local section"):
            self.read()


if __name__ == "__main__":
    unittest.main()
