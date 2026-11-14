#!/usr/bin/env python3
"""Non-expiring S3 CAS mutex shared by import, publication and physical GC.

There is deliberately NO timeout/takeover. A crashed job requires human recovery.
An outer Actions job can hold the mutex across subprocesses using a random token;
children borrow only after comparing the complete owner document and scope.
"""
import contextlib
import json
import os
import re
import secrets
import sys
import boto3
from botocore.exceptions import ClientError

KEY = 'production-publish.lock'
REPOSITORY = 'mowang7426/mowang'


def scope():
    prefix = os.environ.get('R2_TEST_PREFIX', 'repo/')
    if prefix != 'repo/':
        branch = prefix.rstrip('/')
        if (not re.fullmatch(r'catalog-e2e/r2-gc-[A-Za-z0-9_-]+/', prefix) and
                not re.fullmatch(r'catalog-e2e/[A-Za-z0-9_-]+/', prefix)):
            raise ValueError('invalid lock scope')
        if (os.environ.get('R2_TEST_BRANCH') != branch or
                os.environ.get('GITHUB_REF') != 'refs/heads/' + branch or
                os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch' or
                os.environ.get('GITHUB_REPOSITORY') != REPOSITORY):
            raise ValueError('untrusted lock test scope')
    return prefix


def client():
    return boto3.client('s3', endpoint_url='https://' + os.environ['R2_ACCOUNT_ID'] +
                        '.r2.cloudflarestorage.com', region_name='auto')


def document(token, prefix):
    if not re.fullmatch('[0-9a-f]{64}', token):
        raise ValueError('invalid mutex token')
    return json.dumps({'schema': 2, 'token': token, 'prefix': prefix,
        'repository': REPOSITORY, 'run': os.environ.get('GITHUB_RUN_ID', 'local'),
        'attempt': os.environ.get('GITHUB_RUN_ATTEMPT', '1')}, sort_keys=True).encode()


class Mutex:
    def __init__(self, s3, bucket, prefix, token=None, key_prefix=None):
        self.s3, self.bucket, self.key = s3, bucket, (prefix if key_prefix is None else key_prefix) + KEY
        self.token = token or secrets.token_hex(32)
        self.body = document(self.token, prefix)
        self.acquired = False

    def acquire(self):
        self.s3.put_object(Bucket=self.bucket, Key=self.key, Body=self.body,
                           IfNoneMatch='*', ContentType='application/json')
        self.acquired = True
        self.verify()
        return self

    def verify(self):
        response = self.s3.get_object(Bucket=self.bucket, Key=self.key)
        try:
            raw = response['Body'].read()
        finally:
            response['Body'].close()
        if raw != self.body:
            raise RuntimeError('lost shared mutex / wrong owner, run or scope')

    def release(self):
        # Nobody may take over a lock. Under that protocol an unconditional
        # DELETE after verifying ownership is safe (R2 ignores DELETE If-Match).
        self.verify()
        self.s3.delete_object(Bucket=self.bucket, Key=self.key)
        self.acquired = False


@contextlib.contextmanager
def hold(s3=None, bucket=None, prefix=None, key_prefix=None):
    prefix = prefix or scope()
    if prefix != scope():
        raise ValueError('shared mutex scope mismatch')
    borrowed = os.environ.get('R2_SHARED_LOCK_TOKEN')
    lock = Mutex(s3 or client(), bucket or os.environ['R2_BUCKET'], prefix, borrowed, key_prefix)
    if borrowed:
        lock.verify()
    else:
        lock.acquire()
    previous = os.environ.get('R2_SHARED_LOCK_TOKEN')
    os.environ['R2_SHARED_LOCK_TOKEN'] = lock.token
    try:
        yield lock
    finally:
        if previous is None:
            os.environ.pop('R2_SHARED_LOCK_TOKEN', None)
        else:
            os.environ['R2_SHARED_LOCK_TOKEN'] = previous
        if not borrowed:
            lock.release()


def main():
    action = sys.argv[1]
    prefix = scope()
    if action == 'acquire':
        if os.environ.get('R2_SHARED_LOCK_TOKEN'):
            raise RuntimeError('outer mutex already supplied')
        lock = Mutex(client(), os.environ['R2_BUCKET'], prefix).acquire()
        # Only the protected runner's internal environment file receives token.
        with open(os.environ['GITHUB_ENV'], 'a') as env:
            env.write('R2_SHARED_LOCK_TOKEN=' + lock.token + '\n')
    elif action == 'release':
        token = os.environ.get('R2_SHARED_LOCK_TOKEN')
        if not token:
            return  # acquire failed; never release another owner's lock
        Mutex(client(), os.environ['R2_BUCKET'], prefix, token).release()
    else:
        raise ValueError('unknown mutex operation')


if __name__ == '__main__':
    main()
