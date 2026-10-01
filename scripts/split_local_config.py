"""Split private overrides without starting the server or initializing providers."""

import sys
from pathlib import Path


def main():
    project = Path(__file__).resolve().parents[1] / "main/xiaozhi-server"
    sys.path.insert(0, str(project))
    from config.local_config import LocalConfigStore

    store = LocalConfigStore()
    if not store.local_path.is_file():
        raise SystemExit("Missing data/.config.yaml; create it before migration")
    with store.locked():
        before = store.read_unlocked()
        store.write_unlocked(before)
        if store.read_unlocked() != before:
            raise SystemExit("Local configuration changed unexpectedly during migration")
    print("Local overrides split into data/config.d/; all values preserved.")
    print("Original YAML: data/.config.yaml.pre-split.backup")
    print("Unknown roots remain in data/.config.yaml. No restart was performed.")


if __name__ == "__main__":
    main()
