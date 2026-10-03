"""Authorize Google and restore a logical node without materializing runtime data."""

import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1] / "main/xiaozhi-server"
sys.path.insert(0, str(PROJECT))

from config.cloud_recovery import RecoveryError, discover_sources
from config.cloud_restore import restore_cloud_node, select_node, select_source
from config.drive_transport import GoogleDriveTransport
from config.recovery_cli import SafeParser, choose, recovery_passphrase
from config.recovery_oauth import obtain_credentials


def main(argv=None, *, credential_loader=obtain_credentials, transport_factory=GoogleDriveTransport,
         restorer=restore_cloud_node, prompt=None, input_fn=input):
    parser = SafeParser(description=__doc__)
    auth = parser.add_mutually_exclusive_group()
    auth.add_argument("--credentials", type=Path, help="Existing authorized credential JSON")
    auth.add_argument("--oauth-client", type=Path, help="Desktop OAuth client config for the same Drive application")
    parser.add_argument("--source-id", help="Explicit discovered Cloud State source UUID")
    parser.add_argument("--node-id", help="Existing logical node identity")
    parser.add_argument("--activate", action="store_true", help="Explicitly select google_drive for the next startup")
    parser.add_argument("--no-browser", action="store_true", help="Print the authorization URL for loopback OAuth")
    try:
        args = parser.parse_args(argv)
        existing = args.credentials
        if existing is None and args.oauth_client is None and (PROJECT / "data/drive-credentials.json").exists():
            existing = PROJECT / "data/drive-credentials.json"
        if existing is None and args.oauth_client is None:
            print("The same Desktop OAuth client configuration used to create the source is required (--oauth-client).", file=sys.stderr)
            return 4
        credential_bytes, session = credential_loader(existing_path=existing, client_path=args.oauth_client,
                                                      open_browser=not args.no_browser)
        transport = transport_factory(PROJECT / "data/drive-credentials.json", session=session)
        sources = discover_sources(transport)
        source_id = args.source_id
        if source_id is None and len(sources) > 1:
            labels = [f"{item.descriptor['label']} | source_id: {item.descriptor['source_id']}" for item in sources]
            chosen = choose(labels, "Cloud State sources:", input_fn=input_fn)
            source_id = sources[labels.index(chosen)].descriptor["source_id"]
        source = select_source(sources, source_id)
        print(f"Cloud State source: {source.descriptor['label']} | source_id: {source.descriptor['source_id']}")
        node_id = args.node_id
        if node_id is None:
            node_id = choose(sorted(source.descriptor["nodes"]), "Available nodes:", input_fn=input_fn)
        node = select_node(source, node_id)
        print(f"Node: {node}")
        passphrase = recovery_passphrase(prompt=prompt)
        result = restorer(source, node, passphrase, credential_bytes, transport, activate=args.activate)
    except RecoveryError as error:
        print(error.message, file=sys.stderr)
        return 4
    except Exception:
        print(RecoveryError.message, file=sys.stderr)
        return 4
    print("Config: OK\nSoundbank: OK\nMemory: OK\nSecrets: OK")
    print("Bootstrap created.\nNode secrets restored.")
    print(f"Provider: {result.provider}")
    print("Ready to start." if result.provider == "google_drive" else "Ready to switch explicitly in Settings, then restart.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
