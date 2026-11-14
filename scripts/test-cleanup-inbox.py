#!/usr/bin/env python3
"""Local safety regressions; real hosted Actions/R2 E2E is a separate job."""
import copy, os, subprocess, tempfile, unittest
from pathlib import Path
import catalog as c
import importlib.util

def load(name,file):
 s=importlib.util.spec_from_file_location(name,Path(__file__).with_name(file));m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
m=load('cleanup','cleanup-inbox.py');fixture=load('import_fixture','test-catalog-import.py')

class Cleanup(unittest.TestCase):
 setUp=fixture.ImportTests.setUp
 tearDown=fixture.ImportTests.tearDown
 package=fixture.ImportTests.package
 def proof(self):
  rs=sorted(self.records,key=lambda r:r['id']);owner='test-owner'
  return {'schema':1,'success':True,'physical_gc_enabled':False,'catalog_sha256':c.sha(c.encoded(rs)),'index_sha256':c.sha(c.render(rs)),'records':len(rs),'_owner':owner,'verified_objects':{r['object_key']:{'sha256':r['sha256'],'size':r['size'],'head_metadata_verified':True,'full_get_hash_verified':True} for r in rs},'object_attestations':{r['object_key']:{'owner':owner,'owner_version':'1','git_blob':r['source']['git_blob'],'s3_full_get_verified':True,'public_full_get_verified':True} for r in rs}}
 def choose(self,proof=None,excluded=None,records=None,inbox=None):
  return m.candidates(self.records if records is None else records,self.ledger,c.tree_snapshot(self.root) if inbox is None else inbox,self.proof() if proof is None else proof,set() if excluded is None else excluded)
 def test_all_historical_excluded(self):
  rows,count=self.choose(excluded={c.source_token(r['source']) for r in self.records});self.assertEqual((rows,count),([],2))
 def test_replacement_blob_is_future(self):
  excluded={c.source_token(dict(r['source'],git_blob='f'*40)) for r in self.records}
  self.assertEqual(len(self.choose(excluded=excluded)[0]),2)
 def test_exact_current_blob_only(self):
  inbox=c.tree_snapshot(self.root);inbox[next(iter(inbox))]='f'*40
  self.assertEqual(len(self.choose(inbox=inbox)[0]),1)
 def test_missing_source_retained(self): self.assertEqual(self.choose(inbox={})[0],[])
 def test_failed_publish_rejected(self):
  p=self.proof();p['success']=False
  with self.assertRaises(ValueError): self.choose(p)
 def test_index_hash_rejected(self):
  p=self.proof();p['index_sha256']='0'*64
  with self.assertRaises(ValueError): self.choose(p)
 def test_catalog_hash_rejected(self):
  p=self.proof();p['catalog_sha256']='0'*64
  with self.assertRaises(ValueError): self.choose(p)
 def test_missing_get_evidence(self):
  p=self.proof();next(iter(p['object_attestations'].values()))['s3_full_get_verified']=False
  with self.assertRaises(ValueError): self.choose(p)
 def test_missing_public_evidence(self):
  p=self.proof();next(iter(p['object_attestations'].values()))['public_full_get_verified']=False
  with self.assertRaises(ValueError): self.choose(p)
 def test_foreign_owner_rejected(self):
  p=self.proof();next(iter(p['object_attestations'].values()))['owner']='foreign'
  with self.assertRaises(ValueError): self.choose(p)
 def test_size_rejected(self):
  p=self.proof();next(iter(p['verified_objects'].values()))['size']+=1
  with self.assertRaises(ValueError): self.choose(p)
 def test_gc_enabled_rejected(self):
  p=self.proof();p['physical_gc_enabled']=True
  with self.assertRaises(ValueError): self.choose(p)
 def test_exact_nonforce_cleanup_preserves_catalog(self):
  parent=self.api.head('main');rows,_=self.choose();commit=m.apply(self.root,'main',self.api,parent,rows)
  self.assertNotEqual(parent,commit);self.assertEqual(c.tree_snapshot(self.root,commit),{})
  self.assertEqual(m.git(self.root,'rev-parse',parent+':packages'),m.git(self.root,'rev-parse',commit+':packages'))
  self.assertEqual(m.git(self.root,'rev-parse',parent+':catalog-state'),m.git(self.root,'rev-parse',commit+':catalog-state'))
 def test_stale_head_rejected(self):
  parent=self.api.head('main');child=self.api.edit('main',parent,{'unrelated.txt':b'new'})
  fixture.run(self.remote,'update-ref','refs/heads/main',child,parent)
  with self.assertRaises(ValueError): m.apply(self.root,'main',self.api,parent,self.records)
  self.assertEqual(len(c.tree_snapshot(self.remote,child)),2)
 def test_push_race_nonforce_rejected(self):
  parent=self.api.head('main');original=m.exact_commit
  def race(root,parent,rows):
   child=self.api.edit('main',parent,{'unrelated.txt':b'race'})
   fixture.run(self.remote,'update-ref','refs/heads/main',child,parent)
   return original(root,parent,rows)
  m.exact_commit=race
  try:
   with self.assertRaises(subprocess.CalledProcessError): m.apply(self.root,'main',self.api,parent,self.records)
  finally: m.exact_commit=original
  self.assertEqual(len(c.tree_snapshot(self.remote,self.api.head('main'))),2)
 def test_raw_non_utf8_path_exact_tree(self):
  raw=b'debs/invalid-\xff.deb';blob=fixture.run(self.root,'hash-object','-w','--stdin',data=b'fixture')
  subprocess.run(['git','-C',str(self.root),'update-index','--add','-z','--index-info'],input=b'100644 '+blob.encode()+b'\t'+raw+b'\0',check=True)
  fixture.run(self.root,'commit','-m','non utf8')
  parent=fixture.run(self.root,'rev-parse','HEAD')
  row={'source':{'filename_b64':c.b64(raw),'git_blob':blob}}
  commit=m.exact_commit(self.root,parent,[row])
  self.assertNotIn(raw,c.tree_snapshot(self.root,commit));self.assertEqual(len(c.tree_snapshot(self.root,commit)),2)
 def test_baseline_checksum(self):
  self.assertEqual(len(m.baseline(Path(__file__).resolve().parents[1])),1439)
 def test_modified_baseline_fails(self):
  (self.root/'catalog-state/inbox-cleanup-baseline.json').write_text('{}')
  with self.assertRaises(ValueError):m.baseline(self.root)
 def test_noop_does_not_push(self):
  parent=self.api.head('main');self.assertEqual(m.apply(self.root,'main',self.api,parent,[]),parent)
 def test_non_actions_provenance_refused(self):
  from unittest.mock import patch
  with patch.dict(os.environ,{'GITHUB_ACTIONS':'false'}):
   with self.assertRaises((ValueError,KeyError)):m.provenance(self.api,'main')

if __name__=='__main__':unittest.main()
