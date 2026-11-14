#!/usr/bin/env python3
"""Future-only GC engine. Production DELETE intentionally sealed pending protocol integration.

No bucket scans, no CLI receipt/prefix overrides, no adoption of legacy garbage.
The caller must supply authenticated successful publisher evidence, hold the shared
upload/publication/GC lock, and revalidate Git head and live indexes through gate().
"""
import copy
import hashlib
import json
import re
from botocore.exceptions import ClientError

GRACE = 48 * 3600
OWNER = 'mowang7426/mowang-r2-production'
TEST_OWNER = 'mowang7426/mowang-catalog-e2e'
KEY = re.compile(r'debs/([0-9a-f]{64})\.deb\Z')


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def objects(snapshot):
    rows = snapshot['objects']
    if not isinstance(rows, dict):
        raise ValueError('invalid objects')
    for key, row in rows.items():
        match = KEY.fullmatch(key)
        if (not match or row.get('sha256') != match[1] or
                type(row.get('size')) is not int or row['size'] <= 0 or
                type(row.get('refs')) is not int or row['refs'] < 1 or
                row.get('ownership') not in ('managed', 'external')):
            raise ValueError('invalid object evidence')
    return rows


def observe(previous, snapshot, receipt_sha, now):
    """Call only after complete successful publication and authenticated receipt.

    First observation is a strict baseline: *only currently referenced objects*
    become tracked; old manifest garbage/external keys are never adopted. A new
    receipt is required for each missing confirmation; rereading is not a round.
    """
    if not re.fullmatch('[0-9a-f]{64}', receipt_sha):
        raise ValueError('invalid receipt identity')
    current = objects(snapshot)
    if previous is None:
        return {'schema': 1, 'baseline_receipt': receipt_sha, 'last_receipt': receipt_sha,
                'observed_at': now, 'objects': copy.deepcopy(current), 'pending': {}}
    if previous.get('schema') != 1 or now < previous['observed_at']:
        raise ValueError('invalid state or backward clock')
    if previous['last_receipt'] == receipt_sha:
        if previous['objects'] != current:
            raise ValueError('receipt reused for different snapshot')
        return copy.deepcopy(previous)
    result = copy.deepcopy(previous)
    pending = result['pending']
    for key in list(pending):
        if key in current:
            del pending[key]  # Restore cancels event, including its original clock.
        else:
            pending[key]['confirmations'] += 1
    for key, row in previous['objects'].items():
        if key not in current and row['ownership'] == 'managed':
            pending.setdefault(key, {'removed_at': now, 'confirmations': 1,
                'last_present_receipt': previous['last_receipt'],
                'first_absent_receipt': receipt_sha, 'object': copy.deepcopy(row)})
    result.update(last_receipt=receipt_sha, observed_at=now, objects=copy.deepcopy(current))
    return result


def candidates(state, now):
    if now < state['observed_at']:
        raise ValueError('backward clock')
    return {key: copy.deepcopy(event) for key, event in state['pending'].items()
            if key not in state['objects'] and event['confirmations'] >= 2
            and now - event['removed_at'] >= GRACE}


def owned_bytes(s3, bucket, key, owner):
    """Authenticated raw bytes + ownership, rather than local JSON assertions."""
    response = s3.get_object(Bucket=bucket, Key=key)
    try:
        raw = response['Body'].read()
    finally:
        response['Body'].close()
    meta = response.get('Metadata', {})
    if (meta.get('owner') != owner or meta.get('owner-version') != '1' or
            meta.get('sha256') != digest(raw) or meta.get('size') != str(len(raw))):
        raise ValueError('foreign or damaged evidence: ' + key)
    return raw


def delete_isolated(s3, bucket, prefix, owner, state, now, gate):
    """Conditional deletion with S3-authoritative absence verification.

    Hard production seal: even a caller passing arbitrary dispatch input cannot
    enable repo/ deletion. This is the engine's only DEB DELETE call site.
    gate must authenticate current receipt, live indexes, main/catalog and lease,
    and return that same receipt identity. It is repeated before every DELETE.
    """
    if not re.fullmatch(r'catalog-e2e/r2-gc-[A-Za-z0-9_-]+/', prefix) or owner != TEST_OWNER:
        raise ValueError('production GC sealed: shared protocol not integrated')
    result = {'deleted': [], 'protected': [], 'absent': [], 'production_enabled': False}
    for key, event in sorted(candidates(state, now).items()):
        if gate() != state['last_receipt']:
            raise ValueError('live evidence/head/lease changed')
        row = event['object']
        if not KEY.fullmatch(key) or row['sha256'] != KEY.fullmatch(key)[1]:
            raise ValueError('unsafe deletion key')
        try:
            head = s3.head_object(Bucket=bucket, Key=prefix + key)
        except ClientError as error:
            if error.response['ResponseMetadata']['HTTPStatusCode'] == 404:
                result['absent'].append(key)
                continue
            raise
        meta = head.get('Metadata', {})
        if (meta.get('owner') != owner or meta.get('owner-version') != '1' or
                meta.get('sha256') != row['sha256'] or meta.get('size') != str(row['size']) or
                head['ContentLength'] != row['size']):
            result['protected'].append({'key': key, 'reason': 'ownership/size mismatch'})
            continue
        if gate() != state['last_receipt']:
            raise ValueError('live evidence/head/lease changed')
        # S3/R2 single-key ETag precondition, NOT a cross-Git/S3 transaction.
        s3.delete_object(Bucket=bucket, Key=prefix + key, IfMatch=head['ETag'])
        try:
            s3.head_object(Bucket=bucket, Key=prefix + key)
        except ClientError as error:
            if error.response['ResponseMetadata']['HTTPStatusCode'] != 404:
                raise
        else:
            raise ValueError('DELETE did not produce authoritative S3 absence')
        result['deleted'].append(key)
    return result
