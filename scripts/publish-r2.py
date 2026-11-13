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
OWNER='mowang7426/mowang-r2-production'
PREFIX='repo/'
GRACE=48*3600
class ScopedS3:
    """All operations are physically confined to the audited production prefix."""
    def __init__(self, client): self.client=client
    def __getattr__(self, name):
        def call(**kw):
            if 'Key' in kw: kw['Key']=PREFIX+kw['Key']
            return getattr(self.client,name)(**kw)
        return call

def audit_prefix(s3,bucket):
    marker=payload({'owner':OWNER,'version':1,'prefix':PREFIX})
    existing=read(s3,bucket,'ownership.json')
    if existing is not None:
        if existing!=marker: raise ValueError('production prefix has foreign ownership')
        return
    # Before first write list every existing key; never overwrite/adopt an external prefix.
    pages=s3.client.get_paginator('list_objects_v2').paginate(Bucket=bucket,Prefix=PREFIX)
    keys=[r['Key'] for page in pages for r in page.get('Contents',[])]
    if keys: raise ValueError('unclaimed production prefix not empty: '+json.dumps(keys))
    s3.put_object(Bucket=bucket,Key='ownership.json',Body=marker,IfNoneMatch='*')

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
    protected=set(current['objects'])
    for generation in current.get('history',[]): protected.update(generation.get('objects',[]))
    garbage={k:t for k,t in previous.get('garbage',{}).items() if k not in protected}
    for k in previous['objects'].keys()-protected:
        if previous['objects'][k].get('ownership')=='managed': garbage.setdefault(k,now)
    return garbage,sorted(k for k,t in garbage.items() if now-t>=GRACE)

def freshness(commit):
    ref='refs/heads/main'
    prepublish=os.environ.get('GITHUB_REF')=='refs/heads/feat/r2-deb-publish'
    if prepublish: ref='refs/heads/feat/r2-deb-publish'
    r=subprocess.run(['git','ls-remote','origin',ref],check=True,capture_output=True,text=True).stdout.split()
    if not r or r[0]!=commit:raise RuntimeError('branch changed; refuse stale publication')
    if prepublish:
        main=subprocess.run(['git','ls-remote','origin','refs/heads/main'],check=True,capture_output=True,text=True).stdout.split()[0]
        recorded=subprocess.check_output(['git','rev-parse','origin/main'],text=True).strip()
        trees=[subprocess.check_output(['git','rev-parse',x+':debs'],text=True).strip() for x in ('HEAD','origin/main')]
        if main!=recorded or trees[0]!=trees[1]:raise RuntimeError('main advanced or DEB tree differs')

def put_verify(s3,bucket,base,key,data,immutable=False):
    kw={'IfNoneMatch':'*'} if immutable else {}
    existing=read(s3,bucket,key)
    if existing is not None:
        meta=s3.head_object(Bucket=bucket,Key=key).get('Metadata',{})
        if meta.get('owner')!=OWNER or meta.get('owner-version')!='1':raise ValueError('refuse overwrite of external object: '+key)
    s3.put_object(Bucket=bucket,Key=key,Body=data,ContentType=mimetypes.guess_type(key)[0] or 'application/octet-stream',CacheControl='public, max-age=60' if key in ('Release',MANIFEST) or key.startswith('Packages') else 'public, max-age=3600',Metadata={'sha256':sha(data),'size':str(len(data)),'owner':OWNER,'owner-version':'1'},**kw)
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
    section=None
    algorithms={'MD5Sum':'md5','SHA1':'sha1','SHA256':'sha256','SHA512':'sha512'}
    seen=set()
    for line in text.split('\n'):
        if line.endswith(':') and not line.startswith(' '): section=line[:-1]
        elif line.startswith(' ') and section in algorithms:
            digest,size,name=line.split()
            if '/' in name or not name.startswith('Packages'):raise ValueError('unsafe Release file')
            data=(site/name).read_bytes()
            if int(size)!=len(data) or hashlib.new(algorithms[section],data).hexdigest()!=digest:raise ValueError('Release digest mismatch: '+section+' '+name)
            seen.add((section,name))
    for p in site.glob('Packages*'):
        if ('SHA256',p.name) not in seen:raise ValueError('missing Release SHA256')

def main():
    base=os.environ['R2_PUBLIC_BASE_URL'].rstrip('/')
    if not base.endswith('/repo'): raise ValueError('production public base must end /repo')
    bucket=os.environ['R2_BUCKET'];commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
    prepublish=os.environ.get('GITHUB_EVENT_NAME')=='workflow_dispatch' and os.environ.get('GITHUB_REF')=='refs/heads/feat/r2-deb-publish' and os.environ.get('R2_PREPUBLISH')=='true'
    if os.environ.get('GITHUB_REF')!='refs/heads/main' and not prepublish:raise RuntimeError('production only on main or explicitly trusted prepublish dispatch')
    freshness(commit)
    s3=ScopedS3(boto3.client('s3',endpoint_url='https://'+os.environ['R2_ACCOUNT_ID']+'.r2.cloudflarestorage.com',region_name='auto'))
    audit_prefix(s3,bucket)
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
        objects={};files={};sources={}
        for p in sorted(Path('debs').glob('*.deb')):
            with p.open('rb') as f:size,digest=sync.digest(f)
            key='debs/'+digest+'.deb';files[p.name]=key
            if key not in objects:
                sources[key]=p
                objects[key]={'sha256':digest,'size':size,'refs':0}
            objects[key]['refs']+=1
        from concurrent.futures import ThreadPoolExecutor
        def upload(item):
            key,path=item
            owned=previous is not None and previous['objects'].get(key,{}).get('ownership')=='managed'
            return key,sync.sync_one(s3,bucket,path,base,owner=OWNER,previous_owned=owned)
        with ThreadPoolExecutor(max_workers=8) as workers:
            for key,ownership in workers.map(upload,sources.items()):objects[key]['ownership']=ownership
        raw=(site/'Packages').read_bytes().decode('utf-8','surrogateescape')
        rows=[]
        for block in raw.strip('\n').split('\n\n'):
            fields=dict(line.split(': ',1) for line in block.split('\n') if ': ' in line and not line.startswith((' ','\t')))
            rows.append((fields['Filename'],int(fields['Size']),fields['SHA256']))
        expected=collections.Counter((k,objects[k]['size'],objects[k]['sha256']) for k in files.values())
        if collections.Counter(rows)!=expected:raise ValueError('full index coverage differs from repository DEBs')
        current={'schema':1,'base':base,'commit':commit,'files':files,'objects':objects,'release_sha256':sha((site/'Release').read_bytes()),'published_at':datetime.datetime.now(datetime.timezone.utc).timestamp()}
        current['history']=(([{'commit':previous['commit'],'objects':list(previous['objects'])}]+previous.get('history',[]))[:3] if previous else [])
        current['physical_gc_enabled']=False
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
        # Advisory plan ONLY. No production DEB DELETE exists in this publisher.
        # GitHub commits cannot be atomically locked with S3; physical GC is disabled.
        # A future manual protected workflow must revalidate ownership, all retained
        # generations, delay and a reviewed immutable plan before any physical GC.
        print('FULL COVERAGE PASS '+json.dumps({'commit':commit,'packages':len(files),'unique_objects':len(objects),'bytes':sum(x['size'] for x in objects.values()),'deleted':0,'physical_gc_enabled':False,'deferred':len(current['garbage'])}),flush=True)
        Path('r2-publication-result.json').write_bytes(payload(current))
    finally:
        if read(s3,bucket,LOCK)==owner:s3.delete_object(Bucket=bucket,Key=LOCK)
if __name__=='__main__':main()
