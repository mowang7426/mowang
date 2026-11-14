"""Immutable creation claims: owner metadata alone cannot adopt old objects."""
import hashlib
import json
from botocore.exceptions import ClientError


def data(row):
    return json.dumps({'schema': 1, 'sha256': row['sha256'], 'size': row['size'],
        'object_key': 'debs/' + row['sha256'] + '.deb', 'created_by_locked_import': True},
        sort_keys=True).encode()


def key(row):
    return 'object-claims/' + row['sha256'] + '.json'


def verified(s3, bucket, row, owner):
    try:
        response = s3.get_object(Bucket=bucket, Key=key(row))
    except ClientError as error:
        if error.response['ResponseMetadata']['HTTPStatusCode'] == 404:
            return False
        raise
    try:
        raw = response['Body'].read()
    finally:
        response['Body'].close()
    head = s3.head_object(Bucket=bucket, Key=key(row))
    meta = head.get('Metadata', {})
    if (raw != data(row) or head['ContentLength'] != len(raw) or
            meta.get('owner') != owner or meta.get('owner-version') != '1' or
            meta.get('sha256') != hashlib.sha256(raw).hexdigest() or
            meta.get('size') != str(len(raw))):
        raise ValueError('foreign/damaged object creation claim')
    return True


def create(s3, bucket, row, owner):
    if verified(s3, bucket, row, owner):
        return
    raw = data(row)
    s3.put_object(Bucket=bucket, Key=key(row), Body=raw, IfNoneMatch='*',
        Metadata={'owner': owner, 'owner-version': '1',
                  'sha256': hashlib.sha256(raw).hexdigest(), 'size': str(len(raw))})
    if not verified(s3, bucket, row, owner):
        raise ValueError('creation claim readback failed')
