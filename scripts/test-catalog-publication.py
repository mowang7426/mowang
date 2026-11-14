#!/usr/bin/env python3
"""Catalog publisher regression; mocked network tests are NOT production/E2E proof."""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from botocore.exceptions import ClientError

SPEC=importlib.util.spec_from_file_location('publisher',Path(__file__).with_name('publish-r2.py'))
p=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(p)
m=p.catalog_publication
c=m.c
BASE='https://example.r2.dev/repo'

class MemoryS3:
    def __init__(self):self.data={};self.meta={};self.puts=[];self.deleted=[]
    def get_object(self,Bucket,Key):
        if Key not in self.data:raise ClientError({'ResponseMetadata':{'HTTPStatusCode':404},'Error':{'Code':'404'}},'GetObject')
        return {'Body':io.BytesIO(self.data[Key])}
    def head_object(self,Bucket,Key):
        if Key not in self.data:raise ClientError({'ResponseMetadata':{'HTTPStatusCode':404},'Error':{'Code':'404'}},'HeadObject')
        return {'ContentLength':len(self.data[Key]),'Metadata':self.meta[Key], 'ETag':'"'+hashlib.md5(self.data[Key]).hexdigest()+'"'}
    def put_object(self,Bucket,Key,Body,**kw):
        if kw.get('IfNoneMatch')=='*' and Key in self.data:raise ValueError('conditional create conflict')
        if 'IfMatch' in kw and self.head_object(Bucket,Key)['ETag']!=kw['IfMatch']:raise ValueError('conditional update conflict')
        self.data[Key]=Body if isinstance(Body,bytes) else Body.read();self.meta[Key]=kw.get('Metadata',{});self.puts.append(Key)
    def delete_object(self,Bucket,Key):self.deleted.append(Key);self.data.pop(Key,None)


def record(data=b'fixture-1',number=1):
    digest=hashlib.sha256(data).hexdigest()
    blob=hashlib.sha1(b'blob '+str(len(data)).encode()+b'\x00'+data).hexdigest()
    block=(f'Package: test.catalog{number}\nVersion: 1\nArchitecture: iphoneos-arm64\n'
           f'Size: {len(data)}\nSHA256: {digest}\nFilename: debs/source{number}.deb\nDescription: test fixture').encode()
    return c.make_record(block,blob)


def server(records,data):
    s=MemoryS3()
    for r,raw in zip(records,data):
        s.data[r['object_key']]=raw
        s.meta[r['object_key']]={'sha256':r['sha256'],'size':str(r['size']),'owner':p.OWNER,'owner-version':'1'}
    return s


class Objects(unittest.TestCase):
    def setUp(self):
        self.r=record();self.s=server([self.r],[b'fixture-1'])
    def verify(self,records=None,previous=None):
        def public(base,key,size):return m.digest(io.BytesIO(self.s.data[key]),size)
        with patch.object(m,'public_digest',side_effect=public):
            return m.verify_objects(self.s,'bucket',BASE,records or [self.r],p.OWNER,previous)
    def test_complete_actual_digest_evidence(self):
        objects,files,verified,details=self.verify()
        self.assertTrue(verified[self.r['object_key']]['full_get_hash_verified'])
        self.assertTrue(details[self.r['object_key']]['s3_full_get_verified'])
        self.assertEqual(list(files.values()),[self.r['object_key']])
    def test_unstamped_owner_never_adopted(self):
        objects,*_=self.verify();self.assertEqual(objects[self.r['object_key']]['ownership'],'external')
    def test_managed_retained(self):
        previous={'objects':{self.r['object_key']:{'ownership':'managed'}}}
        objects,*_=self.verify(previous=previous);self.assertEqual(objects[self.r['object_key']]['ownership'],'managed')
    def test_shared_content_refs(self):
        r=record(number=2);objects,*_=self.verify([self.r,r]);self.assertEqual(objects[self.r['object_key']]['refs'],2)
    def test_missing_owner_refused(self):
        self.s.meta[self.r['object_key']].pop('owner')
        with self.assertRaises(ValueError):self.verify()
    def test_wrong_owner_refused(self):
        self.s.meta[self.r['object_key']]['owner']='foreign'
        with self.assertRaises(ValueError):self.verify()
    def test_wrong_sha_metadata_refused(self):
        self.s.meta[self.r['object_key']]['sha256']='0'*64
        with self.assertRaises(ValueError):self.verify()
    def test_missing_size_metadata_refused(self):
        self.s.meta[self.r['object_key']].pop('size')
        with self.assertRaises(ValueError):self.verify()
    def test_wrong_content_same_size_refused(self):
        self.s.data[self.r['object_key']]=b'corrupt-1'
        with self.assertRaises(ValueError):self.verify()
    def test_longer_content_refused(self):
        self.s.data[self.r['object_key']]+=b'bad'
        with self.assertRaises(ValueError):self.verify()
    def test_public_hash_mismatch_refused(self):
        with patch.object(m,'public_digest',return_value=(9,'0'*64,'0'*40)):
            with self.assertRaises(ValueError):m.verify_objects(self.s,'b',BASE,[self.r],p.OWNER)
    def test_wrong_git_blob_refused(self):
        r=record();block=c.unb64(r['metadata_b64']);r=c.make_record(block,'1'*40)
        with self.assertRaises(ValueError):self.verify([r])
    def test_same_content_different_blob_refused(self):
        r=c.make_record(c.unb64(record(number=2)['metadata_b64']),'1'*40)
        with self.assertRaises(ValueError):self.verify([self.r,r])
    def test_empty_catalog_refused(self):
        with self.assertRaises(ValueError):m.catalog_objects([])
    def test_arbitrary_key_refused(self):
        r=copy.deepcopy(self.r);r['object_key']='../../foreign'
        with self.assertRaises(ValueError):self.verify([r])
    def test_short_domain_preserves_index_base(self):
        self.assertEqual(m.read_base(m.PRODUCTION_BASE),m.PUBLIC_READ_BASE)
        self.assertEqual(m.read_base(BASE),BASE)
    def test_public_audit_routes_use_canonical_origin(self):
        for key in ('snapshots/'+'a'*40+'/Packages', 'receipts/'+'a'*40+'/123-1.json', 'Packages.zst', '.nojekyll', 'robots.txt', 'style.css', 'app.js'):
            with patch.object(p.subprocess,'check_output',return_value=b'verified') as get:
                self.assertEqual(p.public(m.PRODUCTION_BASE,key),b'verified')
                self.assertEqual(get.call_args.args[0][-1],m.PRODUCTION_BASE+'/'+key)
    def test_public_client_routes_keep_short_domain(self):
        for key in ('Packages','Packages.xz','Release','managed-manifest.json','debs/'+'a'*64+'.deb'):
            with patch.object(p.subprocess,'check_output',return_value=b'verified') as get:
                p.public(m.PRODUCTION_BASE,key)
                self.assertEqual(get.call_args.args[0][-1],m.PUBLIC_READ_BASE+'/'+key)
    def test_public_isolated_origin_unchanged(self):
        with patch.object(p.subprocess,'check_output',return_value=b'verified') as get:
            p.public(BASE,'receipts/'+'a'*40+'/123-1.json')
            self.assertEqual(get.call_args.args[0][-1],BASE+'/receipts/'+'a'*40+'/123-1.json')
    def test_digest_limit(self):
        with self.assertRaises(ValueError):m.digest(io.BytesIO(b'abcd'),3)
    def test_gate_failure_refuses(self):
        def gate():raise RuntimeError('new main')
        with self.assertRaises(RuntimeError):m.verify_objects(self.s,'b',BASE,[self.r],p.OWNER,gate=gate)


class Pipeline(unittest.TestCase):
    def git(self,*args):return subprocess.check_output(['git',*args],stderr=subprocess.DEVNULL).decode().strip()
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.old=os.getcwd();os.chdir(self.tmp.name)
        self.git('init','-q');self.records=[record(),record(b'fixture-2',2)]
        Path('packages').mkdir();Path('catalog-state').mkdir();Path('_site').mkdir()
        self.ledger={'schema':1,'processed':{c.source_token(r['source']):c.ledger_row(r) for r in self.records}}
        for r in self.records:Path('packages',r['id']+'.json').write_bytes(c.encoded(r))
        Path('catalog-state/processed.json').write_bytes(c.encoded(self.ledger))
        self.commit()
        self.s=server(self.records,[b'fixture-1',b'fixture-2'])
        obj,files,_=m.catalog_objects(self.records)
        for row in obj.values():row['ownership']='managed'
        prior={'schema':1,'base':BASE,'commit':'previous','objects':obj,'files':files,'garbage':{}}
        self.s.data[p.MANIFEST]=p.payload(prior);self.s.meta[p.MANIFEST]={'owner':p.OWNER,'owner-version':'1'}
    def tearDown(self):os.chdir(self.old);self.tmp.cleanup()
    def commit(self):
        self.git('add','packages','catalog-state')
        self.git('-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','fixture')
        self.head=self.git('rev-parse','HEAD')
    def run_pipeline(self,failure=None,run_id='123'):
        records,_=c.load_catalog('.')
        Path('_site/Packages').write_bytes(c.render(records));Path('_site/Release').write_bytes(b'fixture release')
        def public(base,key):
            if failure=='public-index' and key=='Packages':return b'wrong'
            return self.s.data[key]
        def public_digest(base,key,size):return m.digest(io.BytesIO(self.s.data[key]),size)
        calls=[0]
        def fresh(commit):
            calls[0]+=1
            if failure=='race' and calls[0]>=3:raise RuntimeError('main advanced')
        env={'R2_PUBLIC_BASE_URL':BASE,'R2_BUCKET':'bucket','R2_ACCOUNT_ID':'account',
             'GITHUB_REF':'refs/heads/main','GITHUB_RUN_ID':run_id,'GITHUB_RUN_ATTEMPT':'1'}
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ,env))
            for obj,name,value in [(p,'ScopedS3',lambda s:s),(p,'audit_prefix',lambda *a:None),
                    (p,'freshness',fresh),(p,'verify_indexes',lambda *a:None),
                    (p,'public',public),(m,'public_digest',public_digest)]:
                stack.enter_context(patch.object(obj,name,side_effect=value))
            stack.enter_context(patch.object(p.boto3,'client',return_value=self.s))
            p.main()
    def test_empty_inbox_publishes_complete_catalog_and_receipt(self):
        self.assertFalse(Path('debs').exists());self.run_pipeline()
        proof=json.loads(Path('r2-publication-receipt.json').read_bytes())
        self.assertTrue(proof['success']);self.assertEqual(proof['commit'],self.head)
        self.assertEqual(proof['records'],2);self.assertEqual(len(proof['verified_objects']),2)
        self.assertFalse(proof['cleanup_enabled']);self.assertEqual(self.s.deleted,[p.LOCK])
        self.assertLess(self.s.puts.index('Release'),self.s.puts.index(p.MANIFEST))
        self.assertEqual(json.loads(self.s.data[p.MANIFEST])['source_of_truth'],'packages/*.json')
    def test_delete_record_unlists_and_never_deletes_r2(self):
        removed=self.records[1];Path('packages',removed['id']+'.json').unlink();self.commit();self.run_pipeline()
        self.assertEqual(len(c.paragraphs(self.s.data['Packages'])),1)
        self.assertIn(removed['object_key'],self.s.data)
        self.assertEqual(self.s.deleted,[p.LOCK])
        self.assertEqual(len(json.loads(Path('r2-publication-receipt.json').read_bytes())['verified_objects']),1)
    def test_uncommitted_deletion_refused(self):
        Path('packages',self.records[1]['id']+'.json').unlink()
        with self.assertRaises(ValueError):self.run_pipeline()
        self.assertNotIn('Release',self.s.puts)
    def test_untracked_record_refused(self):
        r=record(b'fixture-3',3);Path('packages',r['id']+'.json').write_bytes(c.encoded(r))
        self.ledger['processed'][c.source_token(r['source'])]=c.ledger_row(r)
        Path('catalog-state/processed.json').write_bytes(c.encoded(self.ledger))
        with self.assertRaises(ValueError):self.run_pipeline()
    def test_pending_new_blob_refused(self):
        Path('debs').mkdir();Path('debs/new.deb').write_bytes(b'new')
        self.git('add','debs');self.git('-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','new inbox')
        with self.assertRaises(ValueError):self.run_pipeline()
        self.assertNotIn('Release',self.s.puts)
    def test_public_failure_no_success_receipt(self):
        Path('r2-publication-receipt.json').write_bytes(b'old success')
        with self.assertRaises(ValueError):self.run_pipeline('public-index')
        self.assertFalse(Path('r2-publication-receipt.json').exists())
        self.assertFalse(any(key.startswith('receipts/') for key in self.s.puts))
    def test_concurrent_main_no_success_receipt(self):
        with self.assertRaises(RuntimeError):self.run_pipeline('race')
        self.assertFalse(Path('r2-publication-receipt.json').exists());self.assertEqual(self.s.deleted,[p.LOCK])
    def test_head_failure_no_release_no_receipt(self):
        self.s.meta[self.records[0]['object_key']]['owner']='foreign'
        with self.assertRaises(ValueError):self.run_pipeline()
        self.assertNotIn('Release',self.s.puts);self.assertFalse(Path('r2-publication-receipt.json').exists())
    def test_stale_inbox_does_not_resurrect_unlisted_record(self):
        removed=self.records[1];Path('packages',removed['id']+'.json').unlink()
        Path('debs').mkdir();Path('debs/source2.deb').write_bytes(b'fixture-2')
        self.git('add','debs');self.commit();self.run_pipeline()
        self.assertEqual(len(c.paragraphs(self.s.data['Packages'])),1)
        self.assertIn(removed['object_key'],self.s.data)
    def test_receipt_incomplete_object_proof_refused(self):
        Path('_site/Packages').write_bytes(c.render(self.records));Path('_site/Release').write_bytes(b'fixture')
        objects,_,_=m.catalog_objects(self.records)
        with self.assertRaises(ValueError):m.receipt(self.records,self.head,'_site',objects,{}, {},BASE,'123',b'{}')
    def test_receipt_index_mismatch_refused(self):
        Path('_site/Packages').write_bytes(b'wrong');Path('_site/Release').write_bytes(b'fixture')
        with self.assertRaises(ValueError):m.receipt(self.records,self.head,'_site',{}, {},{},BASE,'123',b'{}')

if __name__=='__main__':unittest.main()
