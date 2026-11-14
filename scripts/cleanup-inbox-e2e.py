#!/usr/bin/env python3
"""Real Actions + GitHub + R2 executor acceptance, confined to catalog-e2e/."""
import importlib.util, os, shutil, subprocess, sys, tempfile
from pathlib import Path
import catalog as c

def load(name,file):
 s=importlib.util.spec_from_file_location(name,Path(__file__).with_name(file));m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
m=load('cleanup','cleanup-inbox.py');imp=m.imp
branch=os.environ['R2_TEST_BRANCH'];assert branch.startswith('catalog-e2e/')
api=imp.GitHub(m.REPOSITORY,os.environ['GITHUB_TOKEN'])

def reload(head):
 subprocess.run(['git','fetch','origin',head],check=True)
 subprocess.run(['git','checkout','--detach',head],check=True)

def edit(changes):
 parent=api.head(branch);commit=api.commit(branch,parent,changes);assert commit
 reload(commit);return commit

def publish(label,count):
 for cmd in (['bash','build.sh'],['bash','validate-repo.sh'],['bash','scripts/update-repository.sh'],[sys.executable,'scripts/publish-r2.py']):subprocess.run(cmd,check=True)
 proof=c.loads(Path('r2-publication-receipt.json').read_bytes());assert proof['success'] and proof['records']==count
 Path('e2e-'+label+'.json').write_bytes(c.encoded(proof))
 shutil.copyfile('r2-publication-receipt.json','publication-proof/r2-publication-receipt.json')
 return proof

def execute(ok):
 r=subprocess.run([sys.executable,'scripts/cleanup-inbox.py','--branch',branch],capture_output=True)
 Path('e2e-last-executor.log').write_bytes(r.stdout+r.stderr)
 if (r.returncode==0)!=ok:
  print(r.stdout.decode(errors='replace'));print(r.stderr.decode(errors='replace'));raise AssertionError('executor unexpected result')
 return r

initial=api.head(branch);reload(initial)
rs,ledger=c.load_catalog('.');pending=next(r for r in rs if c.unb64(r['source']['filename_b64'])==b'debs/pending.deb')
old=Path('debs/pending.deb').read_bytes();seed=Path('debs/seed.deb').read_bytes()
# Receipt cannot authorize a newer unrelated head or replaced source blob.
edit({'cleanup-race-proof.txt':b'unrelated newer head\n'})
execute(False);assert len(c.tree_snapshot('.',api.head(branch)))==2
edit({'debs/pending.deb':seed})
execute(False);assert c.tree_snapshot('.',api.head(branch))[b'debs/pending.deb']!=pending['source']['git_blob']
edit({'debs/pending.deb':old})
# Real failed publisher must leave no successful local receipt. No pipeline cleanup
# job can run after failure (needs + success). A forged/failed artifact is rejected.
env=dict(os.environ,R2_PUBLIC_BASE_URL='https://example.invalid/not-authorized')
r=subprocess.run([sys.executable,'scripts/publish-r2.py'],env=env,capture_output=True)
assert r.returncode!=0 and not Path('r2-publication-receipt.json').exists()
fake=c.loads(Path('publication-proof/r2-publication-receipt.json').read_bytes());fake['success']=False
Path('publication-proof/r2-publication-receipt.json').write_bytes(c.encoded(fake));execute(False)
# Add a non-UTF8 source using raw index bytes, never a JSON path API.
rawpath=b'debs/nonutf8-\xff.deb';parent=api.head(branch)
with tempfile.TemporaryDirectory() as t:
 env=dict(os.environ,GIT_INDEX_FILE=t+'/index',GIT_AUTHOR_NAME='Isolated E2E',GIT_AUTHOR_EMAIL='e2e@example.invalid',GIT_COMMITTER_NAME='Isolated E2E',GIT_COMMITTER_EMAIL='e2e@example.invalid')
 m.git('.','read-tree',parent,env=env)
 blob=m.git('.','hash-object','-w','--stdin',input=seed).strip()
 subprocess.run(['git','update-index','--add','-z','--index-info'],input=b'100644 '+blob+b'\t'+rawpath+b'\0',env=env,check=True)
 tree=m.git('.','write-tree',env=env).strip().decode()
 commit=m.git('.','commit-tree',tree,'-p',parent,input=b'test: raw non-UTF8 inbox source\n',env=env).strip().decode()
 subprocess.run(['git','push','origin',commit+':refs/heads/'+branch],check=True)
reload(commit)
result=imp.transaction('.',branch,api,imp.r2_upload());assert result['imported']==1
reload(result['commit']);publish('raw-path-published',3)
# Real GitHub non-fast-forward race: consume fully validated plan, then move head.
proof=m.trusted_receipt(api,branch,'.');parent=api.head(branch)
records,ledger=imp.committed_catalog('.',parent);rows,_=m.candidates(records,ledger,c.tree_snapshot('.',parent),proof,m.baseline('.'))
original=m.exact_commit
race_commit=None
def race(root,parent,rows):
 global race_commit
 race_commit=api.commit(branch,parent,{'cleanup-race-proof.txt':b'CAS sibling race\n'});assert race_commit
 return original(root,parent,rows)
m.exact_commit=race
try:
 try:m.apply('.',branch,api,parent,rows);raise AssertionError('stale CAS accepted')
 except subprocess.CalledProcessError:pass
finally:m.exact_commit=original
reload(race_commit);assert len(c.tree_snapshot('.'))==3
# Republish from the new head, never reuse the stale plan; execute the real CLI.
publish('fresh-after-race',3);before=api.head(branch);execute(True)
result=c.loads(Path('cleanup-result.json').read_bytes());assert result['count']==3 and result['historical_excluded']==0 and result['success']
clean=api.head(branch);reload(clean);assert c.tree_snapshot('.')=={}
assert m.git('.','rev-parse',before+':packages')==m.git('.','rev-parse',clean+':packages')
assert m.git('.','rev-parse',before+':catalog-state')==m.git('.','rev-parse',clean+':catalog-state')
Path('e2e-executor-success.json').write_bytes(c.encoded(result))
publish('empty-inbox',3)
# Source JSON deletion unlists; reappearing processed inbox must not restore it
# and is never a candidate when its record is missing.
parent=api.head(branch);tree=api.request('GET','/git/commits/'+parent)['tree']['sha']
blob=api.request('POST','/git/blobs',{'content':c.b64(old),'encoding':'base64'})['sha']
tree=api.request('POST','/git/trees',{'base_tree':tree,'tree':[{'path':'packages/'+pending['id']+'.json','mode':'100644','type':'blob','sha':None},{'path':'debs/pending.deb','mode':'100644','type':'blob','sha':blob}]})['sha']
commit=api.request('POST','/git/commits',{'message':'test: unlist record and retain stale transport','tree':tree,'parents':[parent]})['sha']
api.request('PATCH','/git/refs/heads/'+branch,{'sha':commit,'force':False});reload(commit)
result=imp.transaction('.',branch,api,imp.r2_upload());assert result['imported']==0
publish('unlisted',2);execute(True)
assert c.tree_snapshot('.',api.head(branch))=={b'debs/pending.deb':pending['source']['git_blob']}
import boto3
s3=boto3.client('s3',endpoint_url='https://'+os.environ['R2_ACCOUNT_ID']+'.r2.cloudflarestorage.com',region_name='auto')
head=s3.head_object(Bucket=os.environ['R2_BUCKET'],Key=os.environ['R2_TEST_PREFIX']+pending['object_key']);assert head['ContentLength']==pending['size']
Path('e2e-cleanup-result.json').write_bytes(c.encoded({'success':True,'run':os.environ['GITHUB_RUN_ID'],'branch':branch,'head':api.head(branch),'import_publish_exact_cleanup':True,'cleaned':3,'non_utf8_raw_git_path':True,'same_content_multiple_refs':True,'empty_inbox_catalog_records':3,'unlisted_catalog_records':2,'unlisted_source_retained':True,'failed_publish_no_receipt':True,'forged_failed_receipt_rejected':True,'stale_head_rejected':True,'new_blob_retained':True,'real_nonforce_push_race_rejected':True,'r2_object_retained':True,'physical_gc_enabled':False}))
