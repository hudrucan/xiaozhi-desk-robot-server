"""Provision Config, Soundbank and Memory into the existing bootstrap Drive source."""

import argparse
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1] / "main/xiaozhi-server"
sys.path.insert(0, str(PROJECT))

from config.bootstrap import load_bootstrap
from config.cloud_provisioning import (
    BootstrapPublicationError, ProvisioningConflict, ProvisioningError,
    ProvisioningReconciliation, ProvisioningWriterMismatch, provision_cloud_state,
)
from config.config_store import LocalConfigStoreAdapter


class _SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse may echo private supplied values. Keep diagnostics fixed.
        raise ProvisioningError()


def main(argv=None, *, provisioner=provision_cloud_state):
    parser = _SafeParser(description=__doc__)
    parser.add_argument("--from-local", required=True, action="store_true",
                        help="Explicitly reconcile this node from committed Local state")
    parser.add_argument("--bootstrap", type=Path, help="Local bootstrap file; provider must remain Local")
    parser.add_argument("--local-config", type=Path, help="Optional Local override root, including config.d")
    try:
        args = parser.parse_args(argv)
        bootstrap = load_bootstrap(args.bootstrap)
        local = LocalConfigStoreAdapter(bootstrap, local_path=args.local_config)
        result = provisioner(local, bootstrap_path=args.bootstrap)
    except ProvisioningConflict as error:
        print(str(error), file=sys.stderr)
        return 3
    except (ProvisioningReconciliation, ProvisioningWriterMismatch) as error:
        print(str(error), file=sys.stderr)
        return 2
    except BootstrapPublicationError as error:
        print(str(error), file=sys.stderr)
        return 4
    except Exception:
        print(ProvisioningError.message, file=sys.stderr)
        return 4
    print(f"Cloud Config revision: {result.config_revision}")
    print("Soundbank assets: verified")
    memory = (f"reused {result.memory_revision}" if result.memory_reused else str(result.memory_revision))
    print(f"Cloud Memory revision: {memory if result.memory_revision is not None else 'inactive'}")
    print("Bootstrap metadata: updated")
    print("Provider: still local")
    print("Ready to switch: yes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
