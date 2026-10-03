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
from config.recovery_cli import recovery_passphrase
from config.cloud_recovery import RecoveryConflict, RecoveryError, backup_cloud_node


class _SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse may echo private supplied values. Keep diagnostics fixed.
        raise ProvisioningError()


def main(argv=None, *, provisioner=None, prompt=None):
    parser = _SafeParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--from-local", action="store_true",
                           help="Explicitly reconcile this node from committed Local state")
    operation.add_argument("--backup-secrets", action="store_true",
                           help="Verify existing Cloud authorities and update only recovery metadata/secrets")
    parser.add_argument("--bootstrap", type=Path, help="Local bootstrap file")
    parser.add_argument("--local-config", type=Path, help="Optional Local override root, including config.d")
    parser.add_argument("--source-label", help="Safe display label for the recoverable Cloud State source")
    parser.add_argument("--rotate-recovery-passphrase", action="store_true",
                        help="Explicitly replace this node's backup using a newly confirmed passphrase")
    try:
        args = parser.parse_args(argv)
        bootstrap = load_bootstrap(args.bootstrap)
        if args.backup_secrets:
            passphrase = recovery_passphrase(confirm=True, prompt=prompt)
            config_revision, memory_revision, descriptor_id = backup_cloud_node(bootstrap, passphrase,
                label=args.source_label, rotate=args.rotate_recovery_passphrase)
            from config.cloud_provisioning import ProvisioningResult
            result = ProvisioningResult(config_revision, memory_revision, memory_revision is not None, descriptor_id)
        elif provisioner is None:
            local = LocalConfigStoreAdapter(bootstrap, local_path=args.local_config)
            passphrase = recovery_passphrase(confirm=True, prompt=prompt)
            result = provision_cloud_state(local, bootstrap_path=args.bootstrap, recovery_passphrase=passphrase,
                source_label=args.source_label, rotate_recovery_passphrase=args.rotate_recovery_passphrase)
        else:
            # An injected orchestration callable owns its passphrase interaction.
            local = LocalConfigStoreAdapter(bootstrap, local_path=args.local_config)
            result = provisioner(local, bootstrap_path=args.bootstrap)
    except RecoveryConflict as error:
        print(str(error), file=sys.stderr)
        return 3
    except ProvisioningConflict as error:
        print(str(error), file=sys.stderr)
        return 3
    except (ProvisioningReconciliation, ProvisioningWriterMismatch) as error:
        print(str(error), file=sys.stderr)
        return 2
    except BootstrapPublicationError as error:
        print(str(error), file=sys.stderr)
        return 4
    except RecoveryError as error:
        print(error.message, file=sys.stderr)
        return 4
    except Exception:
        print(ProvisioningError.message, file=sys.stderr)
        return 4
    print(f"Cloud Config revision: {result.config_revision}")
    print("Soundbank assets: verified")
    memory = (f"reused {result.memory_revision}" if result.memory_reused else str(result.memory_revision))
    print(f"Cloud Memory revision: {memory if result.memory_revision is not None else 'inactive'}")
    print("Bootstrap metadata: unchanged" if args.backup_secrets else "Bootstrap metadata: updated")
    if result.descriptor_file_id is not None:
        print("Cloud State descriptor: verified\nEncrypted node-secret backup: verified")
    print(f"Provider: still {bootstrap['config_provider']}")
    print("Cloud State: ready" if args.backup_secrets else "Ready to switch: yes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
