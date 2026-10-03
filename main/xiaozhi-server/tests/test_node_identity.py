"""Hostname defaults and restore node selection without identity publication."""

import importlib.util
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from config.bootstrap import load_bootstrap
from config.cloud_recovery import DiscoveredSource, RecoveryError
from config.cloud_restore import RestoreResult
from config.node_identity import FALLBACK_NODE_ID, hostname_node_id, normalize_hostname
from config.recovery_cli import choose_node
import test_cloud_provisioning as provisioning_tests


class HostnameBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "absent-data/bootstrap.yaml"

    def test_missing_bootstrap_uses_hostname_without_creating_directory_or_file(self):
        with patch("config.node_identity.socket.gethostname", return_value="deskbox"):
            self.assertEqual(load_bootstrap(self.path), {"config_provider": "local", "node_id": "deskbox"})
        self.assertFalse(self.path.parent.exists())

    def test_existing_bootstrap_without_node_id_uses_hostname_without_rewriting_file(self):
        self.path.parent.mkdir()
        content = b"config_provider: local\n"
        self.path.write_bytes(content)
        with patch("config.node_identity.socket.gethostname", return_value="deskbox"):
            self.assertEqual(load_bootstrap(self.path)["node_id"], "deskbox")
        self.assertEqual(self.path.read_bytes(), content)

    def test_persisted_identity_wins_hostname_and_preserves_exact_legacy_spelling(self):
        self.path.parent.mkdir()
        self.path.write_text("config_provider: local\nnode_id: Persisted Node\n")
        before = self.path.read_bytes()
        with patch("config.node_identity.socket.gethostname", side_effect=OSError("Hostname unavailable")) as hostname:
            self.assertEqual(load_bootstrap(self.path)["node_id"], "Persisted Node")
        hostname.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_hostname_normalization_preserves_case_and_valid_dns_labels(self):
        for value, expected in ((" deskbox. \n", "deskbox"), ("DeskBox.lan.", "DeskBox.lan"),
                                ("desk-box", "desk-box"), ("a", "a")):
            with self.subTest(value=value):
                self.assertEqual(normalize_hostname(value), expected)

    def test_empty_invalid_or_unavailable_hostname_has_safe_deterministic_fallback(self):
        for value in ("", " \n", ".", "..", "bad/path", "desk box", "-deskbox", "deskbox-", "desk..box",
                      "deskbox..", "desk_box", "a" * 64, ("a" * 60 + ".") * 4, "non-ascii-\u2603", None):
            with self.subTest(value=value):
                self.assertEqual(normalize_hostname(value), FALLBACK_NODE_ID)
                self.assertEqual(normalize_hostname(value), normalize_hostname(value))
        self.assertNotEqual(FALLBACK_NODE_ID, "mac-dev")
        with patch("config.node_identity.socket.gethostname", side_effect=OSError("Unavailable")):
            self.assertEqual(hostname_node_id(), FALLBACK_NODE_ID)

    def test_invalid_persisted_identity_is_rejected_not_replaced_by_hostname(self):
        self.path.parent.mkdir()
        for value in (None, "", 42):
            with self.subTest(value=value):
                self.path.write_text(yaml.safe_dump({"node_id": value}))
                with self.assertRaises(ValueError):
                    load_bootstrap(self.path)

    def test_local_provisioning_persists_derived_identity_then_ignores_hostname_changes(self):
        fixture = provisioning_tests.FullStateProvisioningTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        bootstrap = yaml.safe_load(fixture.bootstrap_path.read_bytes())
        bootstrap.pop("node_id")
        fixture.bootstrap_path.write_text(yaml.safe_dump(bootstrap))
        with patch("config.node_identity.socket.gethostname", return_value="test-node"):
            fixture.local.bootstrap = load_bootstrap(fixture.bootstrap_path)
            fixture.run_provision()
        with patch("config.node_identity.socket.gethostname", return_value="renamed-host"):
            persisted = load_bootstrap(fixture.bootstrap_path)
        self.assertEqual(persisted["node_id"], "test-node")
        self.assertEqual(persisted["config_provider"], "local")


class RestoreNodeSelectionTests(unittest.TestCase):
    def test_single_node_auto_selects_without_prompt_or_hostname_lookup(self):
        input_fn = Mock(side_effect=AssertionError("Redundant prompt"))
        with patch("config.node_identity.socket.gethostname", side_effect=AssertionError("Redundant lookup")):
            self.assertEqual(choose_node({"deskbox": {}}, input_fn=input_fn), "deskbox")
        input_fn.assert_not_called()

    def test_matching_hostname_is_enter_default_among_multiple_nodes(self):
        input_fn = Mock(return_value="")
        with patch("config.node_identity.socket.gethostname", return_value="deskbox"), redirect_stdout(io.StringIO()):
            self.assertEqual(choose_node({"other-node": {}, "deskbox": {}}, input_fn=input_fn), "deskbox")
        input_fn.assert_called_once_with("Node ID [deskbox]: ")

    def test_unmatched_hostname_allows_entering_existing_node(self):
        input_fn = Mock(return_value=" other-node ")
        with patch("config.node_identity.socket.gethostname", return_value="freshbox"), redirect_stdout(io.StringIO()):
            self.assertEqual(choose_node({"deskbox": {}, "other-node": {}}, input_fn=input_fn), "other-node")
        input_fn.assert_called_once_with("Node ID (hostname: freshbox): ")

    def test_invalid_input_or_enter_for_unmatched_hostname_rejects(self):
        for value in ("", "new-node", "1", "PRIVATE /private/path"):
            with self.subTest(value=value), patch("config.node_identity.socket.gethostname", return_value="freshbox"), \
                    redirect_stdout(io.StringIO()):
                with self.assertRaises(RecoveryError) as raised:
                    choose_node({"deskbox": {}, "other-node": {}}, input_fn=Mock(return_value=value))
                self.assertNotIn("PRIVATE", str(raised.exception))

    def cli(self):
        path = Path(__file__).resolve().parents[3] / "scripts/restore_cloud_node.py"
        spec = importlib.util.spec_from_file_location("node_identity_restore_cli", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def run_cli(self, args=(), *, hostname="deskbox", typed="", nodes=("deskbox", "other-node")):
        cli = self.cli()
        descriptor = {"schema_version": 1, "source_id": "00000000-0000-0000-0000-000000000001", "label": "Desk robot",
                      "folder_id": "folder", "config_manifest_file_id": "config", "memory_manifest_file_id": None,
                      "nodes": {node: {"secrets": {"file_id": "bundle", "sha256": "0" * 64}} for node in nodes}}
        source = DiscoveredSource("descriptor", "etag", descriptor)
        restorer, input_fn = Mock(return_value=RestoreResult(1, None, "local")), Mock(return_value=typed)
        with patch.object(cli, "discover_sources", return_value=[source]), \
                patch("config.node_identity.socket.gethostname", return_value=hostname), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            status = cli.main(["--credentials", "existing.json", *args],
                credential_loader=Mock(return_value=(b"credentials", Mock())), transport_factory=Mock(),
                restorer=restorer, prompt=Mock(return_value="test passphrase"), input_fn=input_fn)
        return status, restorer, input_fn

    def test_restore_cli_forwards_hostname_default_and_valid_typed_node(self):
        for hostname, typed, expected in (("deskbox", "", "deskbox"), ("freshbox", "other-node", "other-node")):
            with self.subTest(hostname=hostname):
                status, restorer, _ = self.run_cli(hostname=hostname, typed=typed)
                self.assertEqual(status, 0)
                self.assertEqual(restorer.call_args.args[1], expected)
                self.assertFalse(restorer.call_args.kwargs["activate"])

    def test_restore_cli_rejects_unknown_node_before_identity_publication(self):
        status, restorer, _ = self.run_cli(typed="unknown-node")
        self.assertEqual(status, 4)
        restorer.assert_not_called()

    def test_explicit_node_id_bypasses_hostname_default_and_prompt(self):
        status, restorer, input_fn = self.run_cli(["--node-id", "other-node"], hostname="deskbox")
        self.assertEqual(status, 0)
        self.assertEqual(restorer.call_args.args[1], "other-node")
        input_fn.assert_not_called()
        status, restorer, input_fn = self.run_cli(["--node-id", "missing-node"])
        self.assertEqual(status, 4)
        restorer.assert_not_called()
        input_fn.assert_not_called()

    def test_restore_cli_single_node_needs_no_prompt(self):
        status, restorer, input_fn = self.run_cli(hostname="freshbox", nodes=("deskbox",))
        self.assertEqual(status, 0)
        self.assertEqual(restorer.call_args.args[1], "deskbox")
        input_fn.assert_not_called()
