"""Shared cluster desired state against disposable files and a fake Drive."""

import copy
import json
import unittest
from unittest.mock import patch

import yaml

from config.cloud_layers import (
    initial_cloud_object, initial_cluster_object, migrate_cluster_object,
    resolve_layers, shared_cluster, validate_layers,
)
from config.config_loader import load_default_config, merge_configs
from config.config_store import ConfigConflict, canonical_bytes
from config.config_validation import validate_config
from config.google_drive_config import GoogleDriveConfigStore
from core.utils.config_editor import ConfigEditor
import test_cloud_config as fixtures


class ClusterConfigTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CloudConfigTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.drive = self.fixture.drive
        self.defaults = self.fixture.defaults
        self.defaults['cluster'] = {'ingress': {'vip': '192.168.1.186'}}
        self.fixture.default_path.write_text(yaml.safe_dump(self.defaults))
        self.nodes = ['deskb1x', 'deskb2x', 'deskb3x']
        self.v1 = initial_cloud_object(self.fixture.cloud_overrides, self.nodes[0])
        self.v1['layers']['global'] = {'prompt': 'Global prompt'}
        self.v1['layers']['environments'] = {'production': {'tts_timeout': 20}}
        self.v1['layers']['roles'] = {'core': {'asr_min_audio_ms': 400}}
        for node in self.nodes:
            self.v1['layers']['nodes'][node] = {
                'environment': 'production', 'role': 'core',
                'overrides': copy.deepcopy(self.fixture.cloud_overrides),
            }
        self.drive.publish_object(self.v1, 1)
        self.store = self.new_store(self.nodes[0])

    def new_store(self, node):
        bootstrap = {**self.fixture.bootstrap, 'node_id': node}
        return GoogleDriveConfigStore(bootstrap, transport=self.drive,
            cache_dir=self.fixture.directory / ('cloud-' + node),
            secret_provider=self.fixture.secrets, default_path=self.fixture.default_path)

    def current_object(self):
        return json.loads(self.drive.download(self.drive.manifest['config']['file_id']))

    def effective(self, obj, node):
        return merge_configs(*resolve_layers(obj, node, self.defaults))

    def migrate(self):
        self.store.migrate_cluster(self.nodes, apply=True, base_revision=1)
        return self.current_object()

    def test_old_v1_is_readable_and_startup_does_not_migrate(self):
        effective = self.store.prepare_runtime()
        self.assertEqual(effective['tts_timeout'], 20)
        self.assertEqual(self.current_object(), self.v1)
        self.assertEqual(self.drive.uploads, 0)
        with self.store.locked():
            self.assertTrue(self.store.status_unlocked()['cluster_migration_required'])

    def test_preview_apply_equivalence_references_and_active_lkg_untouched(self):
        self.store.prepare_runtime()
        self.store.mark_applied()
        active = (self.store.cache_dir / 'active.json').read_bytes()
        secrets_before = self.fixture.secrets.path.read_bytes()
        before_views = {node: canonical_bytes(self.effective(self.v1, node)) for node in self.nodes}
        with (patch.object(self.fixture.secrets, 'get', side_effect=AssertionError('Secret read')),
             patch.object(self.fixture.secrets, 'export_dataset', side_effect=AssertionError('Secret export')),
             patch.object(self.fixture.secrets, 'put_many', side_effect=AssertionError('Secret write'))):
            preview = self.store.migrate_cluster(list(reversed(self.nodes)))
            self.assertTrue(preview['changed'])
            self.assertFalse(preview['applied'])
            self.assertEqual(self.drive.uploads, 0)
            result = self.store.migrate_cluster(self.nodes, apply=True, base_revision=preview['base_revision'])
        obj = self.current_object()
        self.assertTrue(shared_cluster(obj))
        self.assertEqual(result['desired_revision'], 2)
        self.assertEqual(result['verified_nodes'], self.nodes)
        self.assertEqual(obj['layers']['cluster'], self.fixture.cloud_overrides)
        for scope in ('global', 'environments', 'roles'):
            self.assertEqual(obj['layers'][scope], self.v1['layers'][scope])
        for node in self.nodes:
            self.assertEqual(obj['layers']['nodes'][node]['overrides'], {})
            self.assertEqual(canonical_bytes(self.effective(obj, node)), before_views[node])
            self.assertEqual(obj['layers']['nodes'][node]['environment'], 'production')
        self.assertEqual(self.fixture.secrets.path.read_bytes(), secrets_before)
        self.assertEqual((self.store.cache_dir / 'active.json').read_bytes(), active)
        self.assertEqual(self.store.active_revision, 1)
        self.assertNotIn(b'private-test-key', canonical_bytes(obj))

    def test_migration_is_idempotent_and_preserves_later_node_exceptions(self):
        obj = self.migrate()
        obj['layers']['nodes']['deskb2x']['overrides'] = {'prompt': 'Explicit exception'}
        self.drive.publish_object(obj, 3)
        uploads = self.drive.uploads
        result = self.store.migrate_cluster(self.nodes, apply=True, base_revision=3)
        self.assertFalse(result['changed'])
        self.assertEqual(self.drive.uploads, uploads)
        self.assertEqual(self.current_object(), obj)

    def test_differing_node_overrides_reject_without_upload(self):
        self.v1['layers']['nodes']['deskb2x']['overrides']['prompt'] = 'Different'
        self.drive.publish_object(self.v1, 2)
        with self.assertRaisesRegex(ValueError, 'overrides differ'):
            self.store.migrate_cluster(self.nodes, apply=True, base_revision=2)
        self.assertEqual(self.drive.uploads, 0)
        self.assertEqual(self.current_object(), self.v1)

    def test_canonical_comparison_accepts_different_map_insertion_order(self):
        common = self.v1['layers']['nodes']['deskb1x']['overrides']
        self.v1['layers']['nodes']['deskb2x']['overrides'] = dict(reversed(list(common.items())))
        obj = migrate_cluster_object(self.v1, self.nodes, validate_config, self.defaults)
        self.assertTrue(shared_cluster(obj))

    def test_unselected_node_effective_change_rejects(self):
        self.v1['layers']['nodes']['deskb3x']['overrides'] = {}
        with self.assertRaisesRegex(ValueError, 'effective node configuration'):
            migrate_cluster_object(self.v1, self.nodes[:2], validate_config, self.defaults)

    def test_unselected_node_is_preserved_when_its_existing_exceptions_mask_promotion(self):
        unrelated = self.v1['layers']['nodes']['deskb3x']['overrides']
        unrelated['prompt'] = 'Unrelated exception'
        obj = migrate_cluster_object(self.v1, self.nodes[:2], validate_config, self.defaults)
        self.assertEqual(obj['layers']['nodes']['deskb3x'], self.v1['layers']['nodes']['deskb3x'])
        self.assertEqual(self.effective(obj, 'deskb3x'), self.effective(self.v1, 'deskb3x'))

    def test_stale_migration_base_revision_rejects_before_upload(self):
        self.store.migrate_cluster(self.nodes)
        self.drive.publish_object(self.v1, 2)
        with self.assertRaises(ConfigConflict):
            self.store.migrate_cluster(self.nodes, apply=True, base_revision=1)
        self.assertEqual(self.drive.uploads, 0)

    def test_migration_apply_requires_preview_revision_and_selection(self):
        with self.assertRaises(ValueError):
            self.store.migrate_cluster(self.nodes, apply=True)
        for nodes in ([], ['unknown'], ['deskb1x', 'deskb1x']):
            with self.subTest(nodes=nodes), self.assertRaises(ValueError):
                migrate_cluster_object(self.v1, nodes, validate_config, self.defaults)
        self.assertEqual(self.drive.uploads, 0)

    def test_migration_cas_race_does_not_replace_foreign_manifest(self):
        other = copy.deepcopy(self.v1)
        other['layers']['global']['prompt'] = 'Foreign update'
        self.drive.before_commit = lambda: self.drive.publish_object(other, 2)
        with self.assertRaises(ConfigConflict):
            self.store.migrate_cluster(self.nodes, apply=True, base_revision=1)
        self.assertEqual(self.current_object(), other)
        self.assertEqual(self.drive.uploads, 1)  # Unreferenced immutable object only.

    def test_cluster_precedence_between_role_and_node(self):
        obj = initial_cluster_object({'server': {'port': 8004}}, 'deskb1x')
        obj['layers']['global'] = {'server': {'port': 8001}}
        obj['layers']['environments'] = {'production': {'server': {'port': 8002}}}
        obj['layers']['roles'] = {'core': {'server': {'port': 8003}}}
        obj['layers']['nodes']['deskb1x'].update(environment='production', role='core', overrides={'server': {'port': 8005}})
        self.assertEqual(self.effective(obj, 'deskb1x')['server']['port'], 8005)
        obj['layers']['nodes']['deskb1x']['overrides'] = {}
        self.assertEqual(self.effective(obj, 'deskb1x')['server']['port'], 8004)
        obj['layers']['cluster'] = {}
        self.assertEqual(self.effective(obj, 'deskb1x')['server']['port'], 8003)
        obj['layers']['roles']['core'] = {}
        self.assertEqual(self.effective(obj, 'deskb1x')['server']['port'], 8002)
        obj['layers']['environments']['production'] = {}
        self.assertEqual(self.effective(obj, 'deskb1x')['server']['port'], 8001)
        obj['layers']['global'] = {}
        self.assertEqual(self.effective(obj, 'deskb1x')['server']['port'], 8000)

    def test_settings_two_nodes_edit_one_cluster_layer_and_keep_exceptions(self):
        obj = self.migrate()
        obj['layers']['nodes']['deskb1x']['overrides'] = {'prompt': 'Node exception'}
        self.drive.publish_object(obj, 3)
        assignments = copy.deepcopy(obj['layers']['nodes'])
        first = ConfigEditor(self.store).update({'prompt': 'Shared first'}, base_revision=3)
        self.assertEqual(first['config']['prompt'], 'Node exception')
        second_store = self.new_store('deskb2x')
        second = ConfigEditor(second_store).update({'prompt': 'Shared second'}, base_revision=4)
        current = self.current_object()
        self.assertEqual(current['layers']['cluster']['prompt'], 'Shared second')
        self.assertEqual(current['layers']['nodes'], assignments)
        self.assertEqual(second['config']['prompt'], 'Shared second')
        self.assertEqual(second['configuration_source']['settings_scope'], 'cluster')
        self.assertEqual(second['configuration_source']['environment'], 'production')
        self.assertNotIn('private-test-key', json.dumps(second))

    def test_stale_settings_revision_rejected_before_shared_write(self):
        self.migrate()
        ConfigEditor(self.new_store('deskb2x')).update({'prompt': 'First'}, base_revision=2)
        before = self.current_object()
        uploads = self.drive.uploads
        with self.assertRaises(ConfigConflict):
            ConfigEditor(self.store).update({'prompt': 'Stale'}, base_revision=2)
        self.assertEqual(self.current_object(), before)
        self.assertEqual(self.drive.uploads, uploads)

    def test_changed_plaintext_shared_secret_fails_before_any_secret_or_asset_write(self):
        self.migrate()
        secret_bytes = self.fixture.secrets.path.read_bytes()
        manifest = copy.deepcopy(self.drive.manifest)
        uploads = self.drive.uploads
        with patch.object(self.fixture.secrets, 'put_many', side_effect=AssertionError('Local secret write')):
            with self.assertRaisesRegex(ValueError, 'cannot save plaintext secrets') as error:
                ConfigEditor(self.store).update({'LLM': {'Test': {'api_key': 'changed-private-value'}}}, base_revision=2)
        self.assertNotIn('changed-private-value', str(error.exception))
        self.assertEqual(self.fixture.secrets.path.read_bytes(), secret_bytes)
        self.assertEqual(self.drive.manifest, manifest)
        self.assertEqual(self.drive.uploads, uploads)

    def test_blank_secret_retains_cluster_and_node_exception_references(self):
        obj = self.migrate()
        obj['layers']['nodes']['deskb1x']['overrides'] = {'LLM': {'Test': {'api_key': '${secret:EXCEPTION}'}}}
        self.drive.publish_object(obj, 3)
        ConfigEditor(self.store).update({'LLM': {'Test': {'api_key': ''}}, 'prompt': 'Shared edit'}, base_revision=3)
        current = self.current_object()
        self.assertEqual(current['layers']['cluster']['LLM'], obj['layers']['cluster']['LLM'])
        self.assertEqual(current['layers']['nodes'], obj['layers']['nodes'])

    def test_blank_inherited_reference_is_not_hoisted_into_empty_cluster(self):
        obj = initial_cluster_object({}, 'deskb1x')
        obj['layers']['global'] = {'LLM': {'Test': {'api_key': '${secret:GLOBAL_KEY}'}}}
        self.drive.publish_object(obj, 2)
        ConfigEditor(self.store).update({'LLM': {'Test': {'api_key': ''}}}, base_revision=2)
        self.assertNotIn('api_key', self.current_object()['layers']['cluster'].get('LLM', {}).get('Test', {}))
        self.assertEqual(self.effective(self.current_object(), 'deskb1x')['LLM']['Test']['api_key'], '${secret:GLOBAL_KEY}')

    def test_redacted_list_edit_rejects_when_inherited_secret_cannot_be_retained(self):
        obj = initial_cluster_object({}, 'deskb1x')
        obj['layers']['global'] = {'context_providers': [{'name': 'Shared', 'headers': {'Authorization': '${secret:GLOBAL_KEY}'}}]}
        self.drive.publish_object(obj, 2)
        with self.assertRaisesRegex(ValueError, 'blank secret'):
            ConfigEditor(self.store).update({'context_providers': [{'name': 'Shared', 'headers': {'Authorization': ''}, 'url': 'https://example.invalid'}]}, base_revision=2)
        self.assertEqual(self.drive.uploads, 0)

    def test_local_settings_still_write_local_overrides_and_allow_local_secrets(self):
        local = self.fixture.local()
        result = ConfigEditor(local).update({'prompt': 'Local edit', 'LLM': {'Test': {'api_key': 'local-replacement'}},
                                            'cluster': {'ingress': {'vip': '192.168.1.187'}}})
        with local.locked():
            self.assertEqual(local.read_unlocked()['LLM']['Test']['api_key'], 'local-replacement')
            self.assertEqual(local.read_unlocked()['cluster']['ingress']['vip'], '192.168.1.187')
        self.assertEqual(result['configuration_source']['config_provider'], 'local')
        self.assertEqual(self.drive.uploads, 0)

    def test_vip_default_validation_and_cluster_save(self):
        self.assertEqual(load_default_config()['cluster']['ingress']['vip'], '192.168.1.186')
        self.migrate()
        ConfigEditor(self.store).update({'cluster': {'ingress': {'vip': '192.168.1.187'}}}, base_revision=2)
        for node in self.nodes:
            self.assertEqual(self.effective(self.current_object(), node)['cluster']['ingress']['vip'], '192.168.1.187')
        for value in ('0.0.0.0', '0.0.0.1', '127.0.0.1', '224.0.0.1', '255.255.255.255', '240.0.0.1', '::1', 'not-an-ip', 123, True):
            candidate = copy.deepcopy(self.defaults)
            candidate['cluster']['ingress']['vip'] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'IPv4 unicast'):
                validate_config(candidate)
        candidate['cluster']['ingress'] = {'vip': '192.168.1.186', 'interface': 'wlan0'}
        with self.assertRaisesRegex(ValueError, 'portable vip'):
            validate_config(candidate)

    def test_new_object_contains_no_per_node_runtime_copy(self):
        obj = initial_cluster_object(self.fixture.cloud_overrides, 'deskb1x')
        self.assertEqual(obj['layers']['cluster'], self.fixture.cloud_overrides)
        self.assertEqual(obj['layers']['nodes']['deskb1x']['overrides'], {})
        validate_layers(obj, validate_config, self.defaults)

    def test_schema_version_and_layer_mismatch_is_rejected(self):
        for obj in (dict(self.v1, schema_version=2), dict(initial_cluster_object({}, 'deskb1x'), schema_version=1)):
            with self.assertRaises(ValueError):
                validate_layers(obj, validate_config, self.defaults)

    def test_cli_migration_preview_apply_and_shared_scope_operation(self):
        from pathlib import Path
        import importlib.util
        path = Path(__file__).resolve().parents[3] / 'scripts/cloud_config_admin.py'
        spec = importlib.util.spec_from_file_location('shared_config_admin', path)
        admin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(admin)
        args = admin.build_parser().parse_args(['--base-revision', '1', 'migrate-cluster', '--nodes', *self.nodes, '--apply'])
        self.assertTrue(args.apply)
        preview = admin.run_operation(self.store, 'migrate-cluster', nodes=self.nodes)
        self.assertEqual(preview['base_revision'], 1)
        admin.run_operation(self.store, 'migrate-cluster', nodes=self.nodes, apply=True, base_revision=1)
        admin.run_operation(self.store, 'set-cluster', layer={'prompt': 'Admin shared'}, base_revision=2)
        self.assertEqual(self.current_object()['layers']['cluster'], {'prompt': 'Admin shared'})
