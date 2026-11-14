#!/usr/bin/env python3
"""Real isolated GitHub CAS -> R2 -> receipt integration. Never touches main/repo/."""
import importlib.util, json, os, subprocess, sys, tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import catalog as c

def module(name,file):
    s=importlib.util.spec_from_file_location(name,Path(__file__).with_name(file));m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
imp=module('catalog_import','import-catalog.py')
api=imp.GitHub('mowang7426/mowang',os.environ['GITHUB_TOKEN'])
branch=os.environ['R2_TEST_BRANCH']
assert branch.startswith('catalog-e2e/') and os.environ['R2_TEST_PREFIX']==branch+'/'
assert os.environ['R2_TEST_OWNER']=='mowang7426/mowang-catalog-e2e'

def reload(commit):
    subprocess.run(['git','fetch','--no-tags','origin',commit],check=True)
    subprocess.run(['git','checkout','--detach',commit],check=True)

def stage_commit(message,changes):
    parent=api.head(branch)
    tree=api.request('GET','/git/commits/'+parent)['tree']['sha']
    rows=[]
    for path,data in changes.items():
        blob=api.request('POST','/git/blobs',{'content':c.b64(data),'encoding':'base64'})['sha'] if data is not None else None
        rows.append({'path':path,'mode':'100644','type':'blob','sha':blob})
    tree=api.request('POST','/git/trees',{'base_tree':tree,'tree':rows})['sha']
    commit=api.request('POST','/git/commits',{'message':message,'tree':tree,'parents':[parent]})['sha']
    api.request('PATCH','/git/refs/heads/'+branch,{'sha':commit,'force':False})
    reload(commit)
    return commit

def publish(label,count):
    subprocess.run(['bash','build.sh'],check=True)
    subprocess.run(['bash','validate-repo.sh'],check=True)
    subprocess.run(['bash','scripts/update-repository.sh'],check=True)
    subprocess.run([sys.executable,'scripts/publish-r2.py'],check=True)
    proof=c.loads(Path('r2-publication-receipt.json').read_bytes())
    assert proof['success'] and proof['records']==count and not proof['cleanup_enabled'] and not proof['physical_gc_enabled']
    Path('e2e-'+label+'.json').write_bytes(c.encoded(proof))
    return proof

def seed():
    # Claim an EMPTY isolated prefix before any immutable archive upload. Do not
    # relax publisher's refusal to adopt nonempty/unclaimed foreign prefixes.
    import boto3
    pub=module('catalog_publisher','publish-r2.py')
    pub.PREFIX=os.environ['R2_TEST_PREFIX'];pub.OWNER=os.environ['R2_TEST_OWNER']
    client=boto3.client('s3',endpoint_url='https://'+os.environ['R2_ACCOUNT_ID']+'.r2.cloudflarestorage.com',region_name='auto')
    pub.audit_prefix(pub.ScopedS3(client),os.environ['R2_BUCKET'])
    records=[];packages={}
    with tempfile.TemporaryDirectory() as t:
        root=Path(t)
        for name in ('seed','pending'):
            p=root/name;(p/'DEBIAN').mkdir(parents=True);(p/'usr/share/catalog-e2e').mkdir(parents=True)
            (p/'DEBIAN/control').write_text('Package: catalog-e2e-'+name+'\nVersion: 1.0\nArchitecture: iphoneos-arm\nMaintainer: Isolated Test\nDescription: No scripts isolated catalog test '+name+'\n')
            (p/'usr/share/catalog-e2e'/name).write_text('catalog isolated fixture '+name+'\n')
            deb=root/(name+'.deb')
            subprocess.run(['dpkg-deb','--build',str(p),str(deb)],check=True)
            packages['debs/'+name+'.deb']=deb.read_bytes()
        # Use exactly the production archive parser on a small temporary Git repo.
        subprocess.run(['git','init',str(root/'git')],check=True,stdout=subprocess.DEVNULL)
        repo=root/'git';(repo/'debs').mkdir()
        for path,data in packages.items():(repo/path).write_bytes(data)
        subprocess.run(['git','-C',str(repo),'add','.'],check=True)
        subprocess.run(['git','-C',str(repo),'-c','user.name=E2E','-c','user.email=e2e@example.invalid','commit','-m','fixtures'],check=True,stdout=subprocess.DEVNULL)
        for path,blob in c.tree_snapshot(repo).items():
            with tempfile.TemporaryDirectory() as d:
                record,pkg=imp.archived_record(repo,path,blob,Path(d))
                if path==b'debs/seed.deb':imp.r2_upload()(record,pkg);records.append(record)
    parent=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
    base=api.request('GET','/git/commits/'+parent)['tree']['sha']
    entries=api.request('GET','/git/trees/'+base)['tree']
    entries=[{k:e[k] for k in ('path','mode','type','sha')} for e in entries if e['path'] not in ('debs','packages','catalog-state')]
    for path,data in packages.items():
        blob=api.request('POST','/git/blobs',{'content':c.b64(data),'encoding':'base64'})['sha']
        entries.append({'path':path,'mode':'100644','type':'blob','sha':blob})
    record=records[0]
    data={'packages/'+record['id']+'.json':c.encoded(record),'catalog-state/processed.json':c.encoded({'schema':1,'processed':{c.source_token(record['source']):c.ledger_row(record)}})}
    for path,raw in data.items():
        blob=api.request('POST','/git/blobs',{'content':c.b64(raw),'encoding':'base64'})['sha']
        entries.append({'path':path,'mode':'100644','type':'blob','sha':blob})
    tree=api.request('POST','/git/trees',{'tree':entries})['sha']
    commit=api.request('POST','/git/commits',{'message':'test: seed isolated two script-free archives','tree':tree,'parents':[parent]})['sha']
    api.request('POST','/git/refs',{'ref':'refs/heads/'+branch,'sha':commit})
    print(json.dumps({'branch':branch,'commit':commit,'prefix':os.environ['R2_TEST_PREFIX']}))

if '--seed' in sys.argv:seed();raise SystemExit
result=imp.transaction('.',branch,api,imp.r2_upload());assert result['imported']==1
reload(result['commit']);Path('e2e-import.json').write_bytes(c.encoded(result))
publish('initial',2)
rs,ledger=c.load_catalog('.')
pending=next(r for r in rs if c.unb64(r['source']['filename_b64'])==b'debs/pending.deb')
old=Path('debs/pending.deb').read_bytes()
stage_commit('test: remove isolated inbox only',{'debs/seed.deb':None,'debs/pending.deb':None})
publish('empty-inbox',2)
stage_commit('test: unlist one record and present stale inbox',{'packages/'+pending['id']+'.json':None,'debs/pending.deb':old})
result=imp.transaction('.',branch,api,imp.r2_upload());assert result['imported']==0
reload(result['commit']);publish('unlisted',1)
import boto3
s3=boto3.client('s3',endpoint_url='https://'+os.environ['R2_ACCOUNT_ID']+'.r2.cloudflarestorage.com',region_name='auto')
head=s3.head_object(Bucket=os.environ['R2_BUCKET'],Key=os.environ['R2_TEST_PREFIX']+pending['object_key'])
assert head['ContentLength']==pending['size']
Path('e2e-result.json').write_bytes(c.encoded({'success':True,'branch':branch,'head':result['commit'],'prefix':os.environ['R2_TEST_PREFIX'],'initial_records':2,'empty_inbox_records':2,'unlisted_records':1,'stale_inbox_imported':0,'unlisted_r2_object_retained':True,'cleanup_enabled':False,'physical_gc_enabled':False,'run':os.environ['GITHUB_RUN_ID']}))
