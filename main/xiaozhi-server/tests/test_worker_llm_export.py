"""Cache-only bundle export and private publication; no Drive/NATS/SDK calls."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from export_worker_llm_config import build_bundle, write_private
from core.cluster.llm_config import load_bundle


class ExportTests(unittest.TestCase):
    def store(self):
        provider={'type':'gemini','model_name':'fixture-model','api_key':'${secret:FIXTURE}',
                  'timeout':120,'max_output_tokens':2048,'unused_secret':'${secret:UNRELATED}'}
        config={'selected_module':{'LLM':'GeminiLLM'},'LLM':{'GeminiLLM':provider},
                'ASR':{'Unused':{'api_key':'${secret:MISSING_ASR}'}},'prompt':'Configured prompt',
                'static_soundbank':{'entries':{'hello':{'file':'missing-local.p3'}}}}
        obj={'schema_version':2,'layers':{'global':{},'environments':{},'roles':{},'cluster':{},'nodes':{}}}
        snapshot={'payload':{'manifest':{'revision':6},'object':obj}}
        resolved=[]
        def resolve(value):
            self.assertEqual(set(value),{'LLM'})
            self.assertNotIn('unused_secret',value['LLM'])
            resolved.append(True)
            result=copy.deepcopy(value);result['LLM']['api_key']='fixture-only'
            return result
        self.original=config
        self.resolved=resolved
        self.snapshot=snapshot
        return SimpleNamespace(bootstrap={'node_id':'deskb2x'},
            _read_cache=lambda name:snapshot if name=='desired.json' else None,
            _resolve=lambda value: (config,{}),secrets=SimpleNamespace(resolve=resolve))

    def test_selected_llm_only_no_assets_and_cloud_config_unchanged(self):
        store=self.store();before=copy.deepcopy(self.original)
        value=build_bundle(store)
        self.assertEqual(value['revision'],6)
        self.assertEqual(value['worker_id'],'deskb2x')
        self.assertEqual(value['provider']['timeout'],30)
        self.assertEqual(value['provider']['api_key'],'fixture-only')
        self.assertEqual(self.resolved,[True])
        self.assertEqual(self.original,before)
        self.assertNotIn('static_soundbank',value)
        self.assertNotIn('ASR',value)

    def test_legacy_and_proxy_config_require_explicit_support(self):
        store=self.store()
        self.snapshot['payload']['object']['schema_version']=1
        with self.assertRaises(ValueError):build_bundle(store)
        store=self.store();self.original['LLM']['GeminiLLM']['http_proxy']='http://fixture'
        with self.assertRaises(ValueError):build_bundle(store)
        self.assertEqual(self.resolved,[])

    def test_atomic_private_export_and_wrong_node_rejection(self):
        value=build_bundle(self.store())
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'bundle.json'
            write_private(str(path),value)
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            self.assertEqual(load_bundle(str(path),'deskb2x'),value)
            self.assertEqual(list(Path(directory).iterdir()),[path])
            with self.assertRaises(ValueError):load_bundle(str(path),'deskb3x')
            link=Path(directory)/'link';link.symlink_to(path)
            with self.assertRaises(ValueError):write_private(str(link),value)

    def test_invalid_cached_source_is_not_replaced_with_live_refresh(self):
        store=self.store()
        def invalid(name):raise ValueError('Invalid cache source')
        store._read_cache=invalid
        with self.assertRaises(ValueError):build_bundle(store)
        self.assertEqual(self.resolved,[])

    def test_expected_revision_drift_fails_before_secret_resolution(self):
        store = self.store()
        with self.assertRaises(ValueError):
            build_bundle(store, expected_revision=7)
        self.assertEqual(self.resolved, [])

    def test_unchanged_private_bundle_is_not_rewritten(self):
        value = build_bundle(self.store())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bundle.json'
            self.assertTrue(write_private(str(path), value))
            before = path.stat().st_mtime_ns
            path.chmod(0o640)
            self.assertFalse(write_private(str(path), value))
            self.assertEqual(path.stat().st_mtime_ns, before)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                write_private(str(path), value)


if __name__=='__main__':unittest.main()
