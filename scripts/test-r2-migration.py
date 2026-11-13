#!/usr/bin/env python3
"""Exercise production R2 modules, fail-closed storage paths and real Pages builder."""
import hashlib, importlib.util, io, os, shutil, subprocess, tempfile, unittest
from pathlib import Path
from unittest.mock import Mock, patch
from botocore.exceptions import ClientError
ROOT=Path(__file__).resolve().parents[1]
def load(name):
 s=importlib.util.spec_from_file_location(name,ROOT/'scripts'/(name+'.py'));m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
index=load('index-r2');sync=load('sync-r2')
BASE='https://example.r2.dev'
class MigrationTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.debs=self.root/'debs';self.debs.mkdir();self.deb=self.debs/'中文 \u0085.deb';self.deb.write_bytes(b'original deb bytes');self.sha=hashlib.sha256(self.deb.read_bytes()).hexdigest();self.p=self.root/'Packages'
  self.block=('Package: test\nDescription: bad\udcff keep \u0085 \u2028\n continued\nSize: 18\nFilename: debs/old.deb\nMD5sum: unrelated\nSHA256: '+self.sha+'\nArchitecture: iphoneos-arm64e\n\n').encode('utf-8','surrogateescape');self.p.write_bytes(self.block)
 def rewrite(self):index.rewrite(self.p,BASE,self.debs)
 def test_preserves_every_other_metadata_byte(self):
  self.rewrite();self.assertEqual(self.p.read_bytes(),self.block.replace(b'Filename: debs/old.deb',('Filename: debs/'+self.sha+'.deb').encode()))
 def test_bad_size_fails_without_writing(self):
  bad=self.block.replace(b'Size: 18',b'Size: 999');self.p.write_bytes(bad)
  with self.assertRaises(ValueError):self.rewrite()
  self.assertEqual(self.p.read_bytes(),bad)
 def test_missing_deb_fails(self):
  self.deb.unlink()
  with self.assertRaises(ValueError):self.rewrite()
 def test_missing_entry_fails(self):
  self.p.write_bytes(b'')
  with self.assertRaises(ValueError):self.rewrite()
 def test_duplicate_entry_fails(self):
  self.p.write_bytes(self.block*2)
  with self.assertRaises(ValueError):self.rewrite()
 def test_duplicate_field_fails(self):
  self.p.write_bytes(self.block.replace(b'Size: 18',b'Size: 18\nSize: 18'))
  with self.assertRaises(ValueError):self.rewrite()
 def test_invalid_base_fails(self):
  with self.assertRaises(ValueError):index.rewrite(self.p,'http://insecure',self.debs)
 def storage(self):
  s=Mock();s.head_object.return_value={'ContentLength':18,'Metadata':{'sha256':self.sha,'size':'18'}};s.get_object.return_value={'Body':io.BytesIO(self.deb.read_bytes())};return s
 def curl(self,args,stdout,check):stdout.write(self.deb.read_bytes())
 def test_safe_production_prefix_is_accepted(self):
  index.rewrite(self.p,BASE+'/repo',self.debs)
  self.assertIn(('Filename: debs/'+self.sha+'.deb').encode(),self.p.read_bytes())
 def test_unsafe_prefixes_are_rejected(self):
  for base in [BASE+'/../repo', BASE+'/repo?x=1', BASE+'/a//b', BASE+'/repo#x']:
   with self.subTest(base=base),self.assertRaises(ValueError):index.rewrite(self.p,base,self.debs)
 def test_existing_verified_not_overwritten(self):
  s=self.storage()
  with patch.object(sync.subprocess,'run',side_effect=self.curl):sync.sync_one(s,'bucket',self.deb,BASE)
  s.put_object.assert_not_called()
 def test_new_object_conditional_put(self):
  s=self.storage();s.head_object.side_effect=[ClientError({'ResponseMetadata':{'HTTPStatusCode':404},'Error':{'Code':'404'}},'HeadObject'),s.head_object.return_value]
  with patch.object(sync.subprocess,'run',side_effect=self.curl):sync.sync_one(s,'bucket',self.deb,BASE)
  self.assertEqual(s.put_object.call_args.kwargs['IfNoneMatch'],'*')
 def test_access_denied_never_puts(self):
  s=self.storage();s.head_object.side_effect=ClientError({'ResponseMetadata':{'HTTPStatusCode':403},'Error':{'Code':'403'}},'HeadObject')
  with self.assertRaises(ClientError):sync.sync_one(s,'bucket',self.deb,BASE)
  s.put_object.assert_not_called()
 def test_bad_metadata_fails(self):
  s=self.storage();s.head_object.return_value['Metadata']['size']='19'
  with self.assertRaises(ValueError):sync.sync_one(s,'bucket',self.deb,BASE)
  s.put_object.assert_not_called()
 def test_bad_s3_bytes_fails(self):
  s=self.storage();s.get_object.return_value={'Body':io.BytesIO(b'corrupt')}
  with self.assertRaises(ValueError):sync.sync_one(s,'bucket',self.deb,BASE)
 def test_bad_public_bytes_fails(self):
  s=self.storage()
  with patch.object(sync.subprocess,'run',side_effect=lambda args,stdout,check:stdout.write(b'corrupt')):
   with self.assertRaises(ValueError):sync.sync_one(s,'bucket',self.deb,BASE)
 def test_upload_race_fails_closed(self):
  s=self.storage();s.head_object.side_effect=ClientError({'ResponseMetadata':{'HTTPStatusCode':404},'Error':{'Code':'404'}},'HeadObject');s.put_object.side_effect=RuntimeError('conditional write lost')
  with self.assertRaises(RuntimeError):sync.sync_one(s,'bucket',self.deb,BASE)

@unittest.skipUnless(shutil.which('apt-ftparchive') and shutil.which('dpkg-deb'),'requires actual Debian APT tools')
class PagesIntegration(unittest.TestCase):
 def test_real_deb_build_is_lightweight_and_complete(self):
  with tempfile.TemporaryDirectory() as t:
   r=Path(t);shutil.copytree(ROOT/'scripts',r/'scripts');(r/'debs').mkdir();(r/'assets').mkdir();(r/'assets'/'source-banner.jpg').write_bytes(b'banner fixture');(r/'depictions').mkdir()
   pkg=r/'pkg'/'DEBIAN';pkg.mkdir(parents=True);(pkg/'control').write_text('Package: test.r2\nVersion: 1.0\nArchitecture: iphoneos-arm64e\nMaintainer: Test <test@example.org>\nDescription: test fixture\nSection: Tweaks\n')
   subprocess.run(['dpkg-deb','--build',str(pkg.parent),str(r/'debs'/'test.deb')],check=True,capture_output=True)
   (r/'sileo-featured.json').write_text('{"banners": [{"url":"https://mowang7426.github.io/mowang/assets/source-banner.jpg","title":"莫忘专属备份源"}, {"url":"https://mowang-repo.pages.dev/assets/source-banner-v2.jpg","title":"莫忘专属备份源"}]}')
   (r/'assets'/'source-banner-v2.jpg').write_bytes(b'versioned clear banner fixture')
   env=dict(os.environ,R2_PUBLIC_BASE_URL=BASE,REPOSITORY_NAME='mowang',REPOSITORY_OWNER='mowang7426')
   result=subprocess.run(['bash',str(r/'scripts'/'update-repository.sh')],env=env,capture_output=True)
   self.assertEqual(result.returncode,0,(result.stdout+result.stderr).decode(errors='replace'));site=r/'_site';self.assertFalse((site/'debs').exists());self.assertEqual((site/'assets'/'source-banner.jpg').read_bytes(),b'banner fixture')
   deb=(r/'debs'/'test.deb').read_bytes();self.assertIn(('Filename: debs/'+hashlib.sha256(deb).hexdigest()+'.deb').encode(),(site/'Packages').read_bytes());self.assertIn('莫忘专属备份源'.encode(),(site/'Release').read_bytes());self.assertLess(sum(p.stat().st_size for p in site.rglob('*') if p.is_file()),52428800)
   self.assertEqual((site/'assets'/'source-banner-v2.jpg').read_bytes(),b'versioned clear banner fixture')
   self.assertIn('https://mowang-repo.pages.dev/assets/source-banner-v2.jpg',(site/'sileo-featured.json').read_text())
   # Existing publication stays untouched when artifact validation fails.
   previous=(site/'Packages').read_bytes();env['MAX_PAGES_BYTES']='1';failed=subprocess.run(['bash',str(r/'scripts'/'update-repository.sh')],env=env,capture_output=True);self.assertNotEqual(failed.returncode,0);self.assertEqual((site/'Packages').read_bytes(),previous)

if __name__=='__main__':unittest.main()
