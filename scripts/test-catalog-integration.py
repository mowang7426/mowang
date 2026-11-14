#!/usr/bin/env python3
import importlib.util, os, tempfile, json, base64, unittest
from pathlib import Path
from unittest.mock import patch

def load(file):
 s=importlib.util.spec_from_file_location(file,Path(__file__).with_name(file));m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
p=load('publish-r2.py');v=load('verify-r2-public.py')
class Integration(unittest.TestCase):
 def env(self):
  return {'R2_TEST_PREFIX':'catalog-e2e/test/','R2_TEST_BRANCH':'catalog-e2e/test','R2_TEST_OWNER':'mowang7426/mowang-catalog-e2e','GITHUB_REPOSITORY':'mowang7426/mowang','GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_REF':'refs/heads/catalog-e2e/test'}
 def test_production_default(self):
  with patch.dict(os.environ,{},clear=True):self.assertIsNone(p.isolated_scope())
 def test_trusted_scope(self):
  with patch.dict(os.environ,self.env(),clear=True):self.assertEqual(p.isolated_scope(),('catalog-e2e/test/','catalog-e2e/test'))
 def test_bad_scope_fields(self):
  for k,val in [('R2_TEST_PREFIX','repo/'),('R2_TEST_PREFIX','catalog-e2e/../'),('R2_TEST_BRANCH','main'),('R2_TEST_OWNER','mowang7426/mowang-r2-production'),('GITHUB_REF','refs/heads/main'),('GITHUB_EVENT_NAME','pull_request'),('GITHUB_REPOSITORY','other/repo')]:
   e=self.env();e[k]=val
   with self.subTest(k=k,val=val),patch.dict(os.environ,e,clear=True),self.assertRaises(ValueError):p.isolated_scope()
 def test_legacy_names(self):self.assertEqual(v.source_names({'files':{'abc.deb':'debs/key'}})['debs/key'],['abc.deb'])
 def setup_catalog(self,root):
  (root/'packages').mkdir();(root/'packages/id.json').write_text(json.dumps({'object_key':'debs/key','source':{'filename_b64':base64.b64encode(b'debs/roothide-\xff.deb').decode()}}))
  return {'source_of_truth':'packages/*.json','files':{'id.json':'debs/key'}}
 def test_catalog_names_surrogate(self):
  with tempfile.TemporaryDirectory() as t:
   r=Path(t);m=self.setup_catalog(r);self.assertEqual(v.source_names(m,r)['debs/key'][0].encode('utf-8','surrogateescape'),b'debs/roothide-\xff.deb')
 def test_wrong_object_rejected(self):
  with tempfile.TemporaryDirectory() as t:
   r=Path(t);m=self.setup_catalog(r);m['files']['id.json']='other'
   with self.assertRaises(ValueError):v.source_names(m,r)
 def test_missing_record_rejected(self):
  with tempfile.TemporaryDirectory() as t:
   r=Path(t);m=self.setup_catalog(r);m['files']['missing.json']='debs/key'
   with self.assertRaises(ValueError):v.source_names(m,r)
 def test_catalog_skip_preserves_legacy_guard(self):
  s=Path(__file__).with_name('publish-indexes.sh').read_text()
  self.assertLess(s.index('catalog-build.py mode'),s.index('git add -- Packages'))
  self.assertIn('require_advance',s);self.assertIn('attempt in 1 2 3',s)
if __name__=='__main__':unittest.main()
