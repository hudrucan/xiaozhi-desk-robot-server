"""Explicitly provision a new private Drive source from local configuration."""

import argparse
import sys
from pathlib import Path


def provision_cloud_source(transport, defaults, overrides, node_id, *, folder_id=None, folder_name=None,
                           secret_provider=None):
    from config.cloud_layers import initial_cluster_object
    from config.config_store import canonical_bytes, checksum
    from config.config_validation import validate_config
    from config.google_drive_config import validate_object
    from config.cloud_secrets import LocalSecretStore

    if (folder_id is None) == (folder_name is None):
        raise ValueError("Select an accessible folder ID or create a new app-owned folder")
    secrets = secret_provider or LocalSecretStore(node_id)
    overrides, pending = secrets.externalize(overrides)
    obj = initial_cluster_object(overrides, node_id)
    content = canonical_bytes(obj)
    manifest = {"schema_version": 1, "revision": 1, "config": {
        "file_id": "pending", "sha256": checksum(content),
    }}
    # Validate before creating any remote resource.
    validate_object(content, manifest, validate_config, defaults)
    # Store only locally and durably before any reference can become live.
    secrets.put_many(pending)
    if folder_id is None:
        folder_id = transport.create_folder(folder_name)
    object_id = transport.upload_immutable(folder_id, content, "config-1.json")
    if checksum(transport.download(object_id)) != manifest["config"]["sha256"]:
        raise ValueError("Uploaded configuration checksum mismatch; manifest was not created")
    manifest["config"]["file_id"] = object_id
    manifest_id = transport.upload_immutable(folder_id, canonical_bytes(manifest), "config-manifest.json")
    if transport.download(manifest_id) != canonical_bytes(manifest):
        raise ValueError("Manifest verification failed; provider was not switched")
    return {"folder_id": folder_id, "manifest_file_id": manifest_id}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    folders = parser.add_mutually_exclusive_group(required=True)
    folders.add_argument("--folder-id", help="Folder already accessible to this OAuth app")
    folders.add_argument("--create-folder", metavar="NAME", help="Create a private app-owned My Drive folder")
    parser.add_argument("--credentials-path", required=True)
    parser.add_argument("--node-id", required=True, help="Local bootstrap identity receiving the imported overrides")
    parser.add_argument("--from-local", action="store_true", required=True,
                        help="Import local overrides; move secrets to private local reference storage")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1] / "main/xiaozhi-server"
    sys.path.insert(0, str(project))
    from config.config_loader import load_default_config
    from config.drive_transport import GoogleDriveTransport
    from config.local_config import load_local_config

    import yaml
    try:
        source = provision_cloud_source(
            GoogleDriveTransport(args.credentials_path), load_default_config(), load_local_config(), args.node_id,
            folder_id=args.folder_id, folder_name=args.create_folder,
        )
    except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError):
        # Parser/provider exceptions can carry private local values. Do not print
        # their payloads or traceback during provisioning.
        print("Provisioning failed: check local configuration, secret storage and Drive access. Provider unchanged.",
              file=sys.stderr)
        return 1
    print(f"New folder_id: {source['folder_id']}")
    print(f"New manifest_file_id: {source['manifest_file_id']}")
    print("Set the Drive metadata in data/bootstrap.yaml, then switch source explicitly in Settings.")
    print("Bootstrap and the running provider were not changed. Keep the Drive source private.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
