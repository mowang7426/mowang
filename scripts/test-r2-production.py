#!/usr/bin/env python3
"""Regression of the exact production orchestration, not an independent simulator."""
import contextlib, hashlib, importlib.util, io, json, os, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from botocore.exceptions import ClientError
spec=importlib.util.spec_from_file_location('pub',Path(__file__).with_name('publish-r2.py'));p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)
BASE='https://example.r2.dev'; A='debs/'+'a'*64+'.deb';B='debs/'+'b'*64+'.deb'
def manifest(keys=(A,),refs=1):
 return {'schema':1,'base':BASE,'objects':{k:{'sha256':k[5:-4],'size':3,'refs':refs} for k in keys},'garbage':{}}
class MemoryS3:
 def __init__(self):self.data={};self.meta={};self.deleted=[];self.puts=[]
 def get_object(self,Bucket,Key):
  if Key not in self.data:raise ClientError({'ResponseMetadata':{'HTTPStatusCode':404},'Error':{'Code':'404'}},'GetObject')
  return {'Body':io.BytesIO(self.data[Key])}
 def put_object(self,Bucket,Key,Body,**kw):
  if kw.get('IfNoneMatch')=='*' and Key in self.data:raise RuntimeError('lock conflict')
  self.data[Key]=Body if isinstance(Body,bytes) else Body.read();self.meta[Key]=kw.get('Metadata',{});self.puts.append(Key)
 def head_object(self,Bucket,Key):return {'ContentLength':len(self.data[Key]),'Metadata':self.meta[Key]}
 def delete_object(self,Bucket,Key):self.deleted.append(Key);self.data.pop(Key,None)
class Production(unittest.TestCase):
 def test_initial_no_ownership_no_cleanup(self):self.assertEqual(p.plan(None,manifest(),10),({},[]))
 def test_shared_reference_retains(self):self.assertEqual(p.plan(manifest(refs=2),manifest(refs=1),10),({},[]))
 def test_newly_unreferenced_is_delayed(self):self.assertEqual(p.plan(manifest(),manifest((B,)),10),({A:10},[]))
 def test_48_hour_delay(self):
  old=manifest((B,));old['garbage']={A:1};self.assertEqual(p.plan(old,manifest((B,)),p.GRACE+1)[1],[A])
 def test_readded_cancels_tombstone(self):
  old=manifest((B,));old['garbage']={A:1};self.assertEqual(p.plan(old,manifest((A,B)),p.GRACE+5),({},[]))
 def test_wrong_base_refuses(self):
  old=manifest();old['base']='https://wrong'
  with self.assertRaises(ValueError):p.plan(old,manifest(),10)
 def test_empty_refuses(self):
  with self.assertRaises(ValueError):p.plan(manifest(),manifest(()),10)
 def test_unmanaged_key_refuses(self):
  old=manifest();old['garbage']={'test-runs/x.deb':1}
  with self.assertRaises(ValueError):p.plan(old,manifest(),10)
 def test_missing_previous_objects_refuses(self):
  with self.assertRaises(ValueError):p.plan({},manifest(),10)
 def test_main_race_refuses(self):
  with patch.object(p.subprocess,'run') as r:
   r.return_value.stdout='other refs/heads/main'
   with self.assertRaises(RuntimeError):p.freshness('old')
 def run_pipeline(self,failure=None,prior=True):
  s=MemoryS3();old=manifest((A,));old['garbage']={B:1}
  if prior:s.data[p.MANIFEST]=p.payload(old)
  with tempfile.TemporaryDirectory() as t:
   cwd=os.getcwd();os.chdir(t)
   try:
    d=Path('debs');d.mkdir();(d/'p.deb').write_bytes(b'new');h=hashlib.sha256(b'new').hexdigest();site=Path('_site');site.mkdir()
    (site/'Packages').write_text(f'Package: test\nVersion: 1\nArchitecture: iphoneos-arm64e\nSize: 3\nSHA256: {h}\nFilename: debs/{h}.deb\n\n');(site/'Release').write_text('fixture Release')
    def public(base,key):
     if failure=='public' and key=='Packages':return b'wrong'
     return s.data[key]
    def sync(*args):
     if failure=='sync':raise RuntimeError('sync failure')
    calls=[0]
    def fresh(commit):
     calls[0]+=1
     if failure=='race' and calls[0]>=3:raise RuntimeError('stale commit')
    env={'R2_PUBLIC_BASE_URL':BASE,'R2_BUCKET':'bucket','R2_ACCOUNT_ID':'account','GITHUB_REF':'refs/heads/main'}
    with patch.dict(os.environ,env),patch.object(p.boto3,'client',return_value=s),patch.object(p,'freshness',side_effect=fresh),patch.object(p,'public',side_effect=public),patch.object(p,'verify_indexes'),patch.object(p.sync,'sync_one',side_effect=sync),patch.object(p.subprocess,'check_output',return_value='commit'):
     if failure:
      with self.assertRaises((RuntimeError,ValueError)):p.main()
     else:p.main()
   finally:os.chdir(cwd)
  return s
 def test_full_production_delayed_delete_after_release(self):
  s=self.run_pipeline();self.assertIn(B,s.deleted);self.assertNotIn(A,s.deleted);self.assertLess(s.puts.index('Release'),s.puts.index(p.MANIFEST));self.assertNotIn(p.LOCK,s.data)
 def test_sync_failure_never_publishes_or_deletes(self):
  s=self.run_pipeline('sync');self.assertNotIn('Release',s.puts);self.assertEqual(s.deleted,[p.LOCK])
 def test_public_failure_never_deletes(self):
  s=self.run_pipeline('public');self.assertEqual(s.deleted,[p.LOCK])
 def test_main_race_never_deletes(self):
  s=self.run_pipeline('race');self.assertEqual(s.deleted,[p.LOCK])
 def test_first_production_zero_candidates(self):
  s=self.run_pipeline(prior=False);self.assertEqual(s.deleted,[p.LOCK])
 def test_bad_public_put_fails(self):
  s=MemoryS3()
  with patch.object(p,'public',return_value=b'corrupt'):
   with self.assertRaises(ValueError):p.put_verify(s,'bucket',BASE,'Packages',b'ok')
 def test_distributed_lock_conflict(self):
  s=MemoryS3();s.data[p.LOCK]=b'other'
  with self.assertRaises(RuntimeError):s.put_object(Bucket='b',Key=p.LOCK,Body=b'mine',IfNoneMatch='*')
if __name__=='__main__':unittest.main()
