#!/usr/bin/env python3
"""Additive immutable R2 publication. Verify every byte before returning success."""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import boto3
from botocore.exceptions import ClientError

def digest(stream):
    h, n = hashlib.sha256(), 0
    while True:
        b = stream.read(1024*1024)
        if not b:
            return n, h.hexdigest()
        h.update(b); n += len(b)

def sync_one(s3, bucket, path, public_base, owner=None, previous_owned=False):
    with path.open('rb') as f:
        size, sha = digest(f)
    key = 'debs/'+sha+'.deb'
    metadata = {'sha256': sha, 'size': str(size)}
    created = False
    if owner:
        metadata.update({'owner': owner, 'owner-version': '1'})
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
    except ClientError as e:
        if e.response['ResponseMetadata']['HTTPStatusCode'] != 404:
            raise
        with path.open('rb') as f:
            s3.put_object(Bucket=bucket, Key=key, Body=f, Metadata=metadata, IfNoneMatch='*')
        created = True
        head = s3.head_object(Bucket=bucket, Key=key)
    meta = head.get('Metadata', {})
    if head['ContentLength'] != size or (not owner and meta != metadata) or (owner and any(k in meta and meta[k] != v for k,v in {'sha256':sha,'size':str(size)}.items())):
        raise ValueError('R2 metadata mismatch: '+key)
    managed = bool(owner and (created or previous_owned) and all(meta.get(k)==v for k,v in metadata.items()))
    if owner and created and not managed:
        raise ValueError('new production object lacks required owner/version metadata: '+key)
    response = s3.get_object(Bucket=bucket, Key=key)
    try:
        if digest(response['Body']) != (size, sha):
            raise ValueError('R2 GET bytes mismatch: '+key)
    finally:
        response['Body'].close()
    # Check the exact public URL that APT will consume, not only authenticated S3.
    with tempfile.TemporaryFile() as f:
        subprocess.run(['curl', '--fail', '--silent', '--show-error', '--retry', '5',
                        '--retry-all-errors', public_base.rstrip('/')+'/'+key], stdout=f, check=True)
        f.seek(0)
        if digest(f) != (size, sha):
            raise ValueError('Public GET bytes mismatch: '+key)
    print('Verified '+key+' ('+str(size)+' bytes)', flush=True)
    return 'managed' if managed else 'external'

def main():
    account, bucket, public = (os.environ[k] for k in ['R2_ACCOUNT_ID', 'R2_BUCKET', 'R2_PUBLIC_BASE_URL'])
    paths = sorted(Path('debs').glob('*.deb'))
    if not paths:
        raise ValueError('No deb packages found')
    s3 = boto3.client('s3', endpoint_url='https://'+account+'.r2.cloudflarestorage.com', region_name='auto')
    for path in paths:
        sync_one(s3, bucket, path, public)
    print('R2 coverage verified: '+str(len(paths))+' packages', flush=True)

if __name__ == '__main__':
    main()
