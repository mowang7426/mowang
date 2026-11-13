#!/usr/bin/env python3
"""Serial, fail-closed full-origin publication and manifest-scoped delayed GC."""
import collections, datetime, hashlib, importlib.util, io, json, mimetypes, os, re, subprocess, sys
from pathlib import Path
import boto3
from botocore.exceptions import ClientError
SPEC=importlib.util.spec_from_file_location('sync',Path(__file__).with_name('sync-r2.py'))
sync=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(sync)
MANIFEST='managed-manifest.json'
LOCK='production-publish.lock'
GRACE=48*3600
PAT=r'debs/[0-9a-f]{64}\.deb'
def sha(b): return hashlib.sha256(b).hexdigest()
def payload(x): return json.dumps(x,ensure_ascii=True,sort_keys=True,indent=2).encode()+b'\n'
def read(s3,bucket,key):
    try:
        r=s3.get_object(Bucket=bucket,Key=key)
        try: return r['Body'].read()
        finally: r['Body'].close()
    except ClientError as e:
        if e.response['ResponseMetadata']['HTTPStatusCode']==404:return None
        raise

def public(base,key):
    return subprocess.check_output(['curl','--fail','--silent','--show-error','--retry','5','--retry-all-errors',base.rstrip('/')+'/'+key])

def validate(m,base):
    if not isinstance(m,dict) or m.get('schema')!=1 or m.get('base')!=base or not m.get('objects'):
        raise ValueError('invalid/empty managed manifest')
    for key,row in m['objects'].items():
        if not re.fullmatch(PAT,key) or row.get('sha256')!=key[5:-4] or not isinstance(row.get('size'),int) or row['size']<=0 or row.get('refs',0)<1:
            raise ValueError('invalid managed object')
    for key,t in m.get('garbage',{}).items():
        if not re.fullmatch(PAT,key) or not isinstance(t,(float,int)) or key in m['objects']:
            raise ValueError('invalid tombstone')
    return m

def plan(previous,current,now):
    validate(current,current['base'])
    if previous is None:return {},[]
    validate(previous,current['base'])
    garbage={k:t for k,t in previous.get('garbage',{}).items() if k not in current['objects']}
    for k in previous['objects'].keys()-current['objects'].keys():garbage.setdefault(k,now)
    return garbage,sorted(k for k,t in garbage.items() if now-t>=GRACE)

def freshness(commit):
    r=subprocess.run(['git','ls-remote','origin','refs/heads/main'],check=True,capture_output=True,text=True).stdout.split()
    if not r or r[0]!=commit:raise RuntimeError('main changed; refuse publication/cleanup')

def put_verify(s3,bucket,base,key,data,immutable=False):
    kw={'IfNoneMatch':'*'} if immutable else {}
    s3.put_object(Bucket=bucket,Key=key,Body=data,ContentType=mimetypes.guess_type(key)[0] or 'application/octet-stream',CacheControl='public, max-age=60' if key in ('Release',MANIFEST) or key.startswith('Packages') else 'public, max-age=3600',Metadata={'sha256':sha(data),'size':str(len(data))},**kw)
    h=s3.head_object(Bucket=bucket,Key=key)
    if h['ContentLength']!=len(data) or h.get('Metadata',{}).get('sha256')!=sha(data) or read(s3,bucket,key)!=data or public(base,key)!=data:
        raise ValueError('published object verification failed: '+key)
    print('Published and S3/public verified '+key+' '+sha(data),flush=True)

def verify_indexes(site):
    import gzip,bz2,lzma
    raw=(site/'Packages').read_bytes()
    if not raw:raise ValueError('empty index')
    for ext,fn in [('gz',gzip.decompress),('bz2',bz2.decompress),('xz',lzma.decompress)]:
        if fn((site/('Packages.'+ext)).read_bytes())!=raw:raise ValueError('compression mismatch')
    text=(site/'Release').read_text()
    for field in ['Origin: 莫忘专属源','Label: 莫忘专属源','Components: main','Suite: stable','Codename:','Date:','Architectures:','SHA256:']:
        if field not in text:raise ValueError('missing Release field '+field)
    for p in site.glob('Packages*'):
        if not re.search(r'^\s+'+sha(p.read_bytes())+r'\s+'+str(p.stat().st_size)+r'\s+'+re.escape(p.name)+r'$',text,re.M):raise ValueError('Release checksum mismatch')

def main():
    base=os.environ['R2_PUBLIC_BASE_URL'].rstrip('/')
    bucket=os.environ['R2_BUCKET'];commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
    if os.environ.get('GITHUB_REF')!='refs/heads/main':raise RuntimeError('production only on main')
    freshness(commit)
    s3=boto3.client('s3',endpoint_url='https://'+os.environ['R2_ACCOUNT_ID']+'.r2.cloudflarestorage.com',region_name='auto')
    owner=payload({'commit':commit,'run':os.environ.get('GITHUB_RUN_ID')})
    # No expiry takeover: a killed runner leaves a fail-closed lock for operator inspection.
    s3.put_object(Bucket=bucket,Key=LOCK,Body=owner,IfNoneMatch='*')
    def gate():
        freshness(commit)
        if read(s3,bucket,LOCK)!=owner:raise RuntimeError('lost distributed lock')
    try:
        prior=read(s3,bucket,MANIFEST)
        previous=validate(json.loads(prior),base) if prior is not None else None
        site=Path('_site');verify_indexes(site)
        objects={};files={}
        for p in sorted(Path('debs').glob('*.deb')):
            with p.open('rb') as f:size,digest=sync.digest(f)
            key='debs/'+digest+'.deb';files[p.name]=key
            if key not in objects:
                sync.sync_one(s3,bucket,p,base)
                objects[key]={'sha256':digest,'size':size,'refs':0}
            objects[key]['refs']+=1
        raw=(site/'Packages').read_bytes().decode('utf-8','surrogateescape')
        rows=[]
        for block in raw.strip('\n').split('\n\n'):
            fields=dict(line.split(': ',1) for line in block.split('\n') if ': ' in line and not line.startswith((' ','\t')))
            rows.append((fields['Filename'],int(fields['Size']),fields['SHA256']))
        expected=collections.Counter((k,objects[k]['size'],objects[k]['sha256']) for k in files.values())
        if collections.Counter(rows)!=expected:raise ValueError('full index coverage differs from repository DEBs')
        current={'schema':1,'base':base,'commit':commit,'files':files,'objects':objects,'release_sha256':sha((site/'Release').read_bytes()),'published_at':datetime.datetime.now(datetime.timezone.utc).timestamp()}
        garbage,candidates=plan(previous,current,current['published_at']);current['garbage']=garbage
        gate()
        # Keep immutable per-version index snapshots. Root multi-object PUT is not atomic.
        for p in sorted(site.iterdir()):
            if p.is_file() and (p.name.startswith('Packages') or p.name=='Release'):
                snap='snapshots/'+commit+'/'+p.name
                existing=read(s3,bucket,snap)
                if existing is None:put_verify(s3,bucket,base,snap,p.read_bytes())
                elif existing!=p.read_bytes():print('Snapshot already retained from earlier run: '+snap,flush=True)
        for p in sorted(site.rglob('*')):
            if p.is_file() and p.name!='Release':put_verify(s3,bucket,base,p.relative_to(site).as_posix(),p.read_bytes())
        gate();put_verify(s3,bucket,base,'Release',(site/'Release').read_bytes())
        # Read all public indexes back after Release, not just PUT responses.
        for p in site.glob('Packages*'):
            if public(base,p.name)!=p.read_bytes():raise ValueError('post-Release index mismatch')
        gate();put_verify(s3,bucket,base,MANIFEST,payload(current))
        print('CLEANUP PLAN '+json.dumps({'first_publication':previous is None,'candidates':candidates,'deferred':garbage},sort_keys=True),flush=True)
        # Only a prior successfully published manifest can grant ownership; no bucket listing.
        for key in candidates:
            gate()
            if public(base,'Release')!=(site/'Release').read_bytes() or public(base,MANIFEST)!=payload(current):raise ValueError('published generation changed before cleanup')
            s3.delete_object(Bucket=bucket,Key=key)
            if read(s3,bucket,key) is not None:raise ValueError('delete not verified')
            del current['garbage'][key]
            print('Deleted previously managed unreferenced object '+key,flush=True)
        if candidates:gate();put_verify(s3,bucket,base,MANIFEST,payload(current))
        print('FULL COVERAGE PASS '+json.dumps({'commit':commit,'packages':len(files),'unique_objects':len(objects),'bytes':sum(x['size'] for x in objects.values()),'deleted':len(candidates),'deferred':len(current['garbage'])}),flush=True)
        Path('r2-publication-result.json').write_bytes(payload(current))
    finally:
        if read(s3,bucket,LOCK)==owner:s3.delete_object(Bucket=bucket,Key=LOCK)
if __name__=='__main__':main()
