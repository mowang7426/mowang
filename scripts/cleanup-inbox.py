#!/usr/bin/env python3
"""Trusted same-run publication -> exact non-force Git inbox CAS. Never R2 GC.

No receipt CLI input exists: read the named artifact from this workflow run and
compare it byte-for-byte with the immutable owner-managed S3 receipt. The caller
must be the downstream cleanup job after the trusted publisher job succeeded.
"""
import argparse, importlib.util, os, re, subprocess, tempfile, urllib.error
from pathlib import Path
import catalog as c

BASELINE_SHA = 'd85a54d532bf8809fab107c3d139aded5aa62c6def7651b717483d3159312216'
REPOSITORY = 'mowang7426/mowang'
WORKFLOW = '.github/workflows/update-repository.yml'
CANONICAL = 'https://pub-f47a5a9b45724684be0f3f006e95e609.r2.dev'

def module(name, filename):
    spec=importlib.util.spec_from_file_location(name,Path(__file__).with_name(filename))
    obj=importlib.util.module_from_spec(spec);spec.loader.exec_module(obj);return obj
imp=module('cleanup_import','import-catalog.py')

def git(root,*args,**kw):
    return subprocess.check_output(['git','-C',str(root),*args],**kw)

def baseline(root):
    raw=(Path(root)/'catalog-state/inbox-cleanup-baseline.json').read_bytes()
    if c.sha(raw)!=BASELINE_SHA: raise ValueError('historical exclusion baseline changed')
    value=c.loads(raw)
    if len(value['sources'])!=1439 or value['mode']!='future-only': raise ValueError('invalid exclusion baseline')
    return {c.source_token(s) for s in value['sources']}

def candidates(records, ledger, inbox, proof, excluded):
    ordered=sorted(records,key=lambda r:r['id'])
    if (proof.get('schema')!=1 or proof.get('success') is not True or
        proof.get('physical_gc_enabled') is not False or
        proof.get('catalog_sha256')!=c.sha(c.encoded(ordered)) or
        proof.get('index_sha256')!=c.sha(c.render(ordered)) or
        proof.get('records')!=len(records)):
        raise ValueError('receipt/catalog/index mismatch or unsuccessful publication')
    rows=[];historical=0
    for record in ordered:
        src=record['source'];path=c.unb64(src['filename_b64']);c.filename(path)
        # c.filename is stricter than a prefix check; only a direct debs/*.deb.
        if not re.fullmatch(rb'debs/[^/]+\.deb',path): raise ValueError('not a direct inbox archive')
        if ledger['processed'].get(c.source_token(src))!=c.ledger_row(record): raise ValueError('ledger mismatch')
        if inbox.get(path)!=src['git_blob']: continue
        if c.source_token(src) in excluded:
            historical+=1;continue
        key=record['object_key'];v=proof['verified_objects'].get(key);a=proof['object_attestations'].get(key,{})
        expected={'sha256':record['sha256'],'size':record['size'],'head_metadata_verified':True,'full_get_hash_verified':True}
        if v!=expected or a.get('git_blob')!=src['git_blob'] or a.get('owner_version')!='1' or a.get('owner')!=proof['_owner'] or a.get('s3_full_get_verified') is not True or a.get('public_full_get_verified') is not True:
            raise ValueError('missing strong R2 evidence')
        rows.append(record)
    return rows,historical

def exact_commit(root,parent,rows):
    """NUL-delimited local index preserves non-UTF8 paths (no GitHub JSON paths)."""
    with tempfile.TemporaryDirectory(prefix='inbox-cas-') as tmp:
        env=dict(os.environ,GIT_INDEX_FILE=tmp+'/index')
        git(root,'read-tree',parent,env=env)
        data=b''.join(b'0 '+b'0'*40+b'\t'+c.unb64(r['source']['filename_b64'])+b'\0' for r in rows)
        subprocess.run(['git','-C',str(root),'update-index','-z','--index-info'],input=data,env=env,check=True)
        tree=git(root,'write-tree',env=env).strip().decode()
        # Prove exactly the planned deletions, never catalog/ledger/baseline edits.
        raw=git(root,'diff-tree','--no-commit-id','--name-status','-r','-z',parent,tree).split(b'\0')
        expected={c.unb64(r['source']['filename_b64']) for r in rows}
        if raw[-1:]!=[b''] or set(raw[1:-1:2])!=expected or raw[0:-1:2]!=[b'D']*len(rows):
            raise ValueError('tree differs outside exact planned deletions')
        env.update(GIT_AUTHOR_NAME='catalog-inbox[bot]',GIT_AUTHOR_EMAIL='catalog-inbox@users.noreply.github.com',GIT_COMMITTER_NAME='catalog-inbox[bot]',GIT_COMMITTER_EMAIL='catalog-inbox@users.noreply.github.com')
        return git(root,'commit-tree',tree,'-p',parent,input=b'catalog: remove published verified inbox transports\n',env=env).strip().decode()

def apply(root,branch,api,parent,rows):
    if api.head(branch)!=parent: raise ValueError('head changed: discard plan and republish; no retry')
    if not rows: return parent
    current=c.tree_snapshot(root,parent)
    if any(current.get(c.unb64(r['source']['filename_b64']))!=r['source']['git_blob'] for r in rows): raise ValueError('blob changed')
    commit=exact_commit(root,parent,rows)
    # A sibling concurrent commit cannot be fast-forwarded; do not force/retry.
    subprocess.run(['git','-C',str(root),'push','origin',commit+':refs/heads/'+branch],check=True)
    if api.head(branch)!=commit: raise ValueError('post-CAS head changed; inspect evidence, never retry stale plan')
    if git(root,'rev-parse',parent+':packages').strip()!=git(root,'rev-parse',commit+':packages').strip(): raise ValueError('catalog tree changed')
    if git(root,'rev-parse',parent+':catalog-state').strip()!=git(root,'rev-parse',commit+':catalog-state').strip(): raise ValueError('state tree changed')
    return commit

def provenance(api,branch):
    run=os.environ['GITHUB_RUN_ID'];attempt=os.environ['GITHUB_RUN_ATTEMPT']
    if os.environ.get('GITHUB_ACTIONS')!='true' or os.environ.get('GITHUB_REPOSITORY')!=REPOSITORY or not run.isdigit() or not attempt.isdigit(): raise ValueError('not a trusted Actions context')
    isolated=bool(re.fullmatch(r'catalog-e2e/[A-Za-z0-9_-]+',branch))
    if branch!='main' and not isolated: raise ValueError('untrusted branch')
    info=api.request('GET','/actions/runs/'+run)
    if info['path']!=WORKFLOW or info['head_branch']!=branch or info['run_attempt']!=int(attempt) or info['event'] not in ('push','schedule','workflow_dispatch'): raise ValueError('untrusted run provenance')
    jobs=api.request('GET','/actions/runs/'+run+'/attempts/'+attempt+'/jobs?per_page=100')['jobs']
    required='isolated_publish' if isolated else 'publish'
    if not any(j['name']==required and j['conclusion']=='success' for j in jobs): raise ValueError('publisher job not successful')
    return run,attempt,isolated

def trusted_receipt(api,branch,root):
    import boto3
    run,attempt,isolated=provenance(api,branch)
    # download-artifact is pinned to this run by the workflow, not dispatch input.
    raw=(Path(root)/'publication-proof/r2-publication-receipt.json').read_bytes();proof=c.loads(raw)
    commit=proof.get('commit','')
    if not re.fullmatch(c.BLOB,commit) or proof.get('run_id')!=run: raise ValueError('receipt run/commit mismatch')
    prefix=branch+'/' if isolated else 'repo/'
    owner='mowang7426/mowang-catalog-e2e' if isolated else 'mowang7426/mowang-r2-production'
    if proof.get('base')!=CANONICAL+'/'+prefix.rstrip('/'): raise ValueError('receipt base mismatch')
    client=boto3.client('s3',endpoint_url='https://'+os.environ['R2_ACCOUNT_ID']+'.r2.cloudflarestorage.com',region_name='auto')
    key=prefix+'receipts/'+commit+'/'+run+'-'+attempt+'.json'
    head=client.head_object(Bucket=os.environ['R2_BUCKET'],Key=key)
    metadata=head.get('Metadata',{})
    if head['ContentLength']!=len(raw) or metadata!={**metadata,'sha256':c.sha(raw),'size':str(len(raw)),'owner':owner,'owner-version':'1'}: raise ValueError('receipt S3 ownership metadata mismatch')
    response=client.get_object(Bucket=os.environ['R2_BUCKET'],Key=key)
    try:
        if response['Body'].read()!=raw: raise ValueError('artifact != immutable owner-managed receipt')
    finally: response['Body'].close()
    # Owner-managed index/manifest currently must still be this publication.
    for name,field in [('Packages','index_sha256'),('managed-manifest.json','manifest_sha256'),('Release','release_sha256')]:
        response=client.get_object(Bucket=os.environ['R2_BUCKET'],Key=prefix+name)
        try:
            if c.sha(response['Body'].read())!=proof[field]: raise ValueError('live publication moved')
        finally: response['Body'].close()
    proof['_owner']=owner
    return proof

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--branch',default=os.environ.get('GITHUB_REF_NAME','main'));p.add_argument('--root',default='.')
    p.add_argument('--approved-plan-sha',default='');p.add_argument('--approved-base-head',default='');args=p.parse_args()
    root=Path(args.root);api=imp.GitHub(REPOSITORY,os.environ['GITHUB_TOKEN'])
    proof=trusted_receipt(api,args.branch,root);parent=api.head(args.branch)
    if parent!=proof['commit']: raise ValueError('stale receipt head: fail closed, dispatch fresh publication')
    subprocess.run(['git','-C',str(root),'fetch','--no-tags','origin',parent],check=True)
    records,ledger=imp.committed_catalog(root,parent);inbox=c.tree_snapshot(root,parent)
    excluded=baseline(root);mode='future-only'
    plan=c.cleanup_plan(records,ledger,inbox,parent,git(root,'rev-parse',parent+':debs').strip().decode())
    if args.approved_plan_sha or args.approved_base_head:
        if args.approved_base_head!=parent or args.approved_plan_sha!=c.sha(c.encoded(plan)): raise ValueError('historical approval plan SHA/head mismatch')
        excluded=set();mode='explicit-approved-plan'
    rows,historical=candidates(records,ledger,inbox,proof,excluded)
    result={'schema':1,'success':False,'mode':mode,'publication_commit':parent,'run_id':proof['run_id'],'plan_sha256':c.sha(c.encoded(plan)),'historical_excluded':historical,'count':len(rows),'bytes':sum(r['size'] for r in rows),'sources':[r['source'] for r in rows],'physical_gc_enabled':False}
    Path('cleanup-execution-plan.json').write_bytes(c.encoded(result))
    commit=apply(root,args.branch,api,parent,rows)
    result.update(success=True,cleanup_commit=commit,catalog_preserved=True,ledger_preserved=True,cleanup_enabled=True)
    Path('cleanup-result.json').write_bytes(c.encoded(result));print(c.encoded({k:v for k,v in result.items() if k!='sources'}).decode())

if __name__=='__main__': main()
