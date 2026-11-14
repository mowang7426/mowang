#!/usr/bin/env python3
"""Real R2 engine contract E2E; not an APT publisher acceptance substitute.

Uses a fresh test branch/prefix only. Test clock advances in memory, never via
production env overrides. No global cleanup and no writes under repo/.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import urllib.error
import urllib.request
import uuid
import boto3
from botocore.exceptions import ClientError

spec = importlib.util.spec_from_file_location('gc', Path(__file__).with_name('r2-physical-gc.py'))
gc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gc)
branch = subprocess.check_output(['git', 'branch', '--show-current'], text=True).strip()
if not __import__('re').fullmatch(r'catalog-e2e/r2-gc-[A-Za-z0-9_-]+', branch):
    raise ValueError('execute only on a dedicated r2-gc test branch')
prefix = branch + '/'
base = 'https://pub-f47a5a9b45724684be0f3f006e95e609.r2.dev/' + prefix
client = boto3.client('s3', endpoint_url='https://f68ff63ce6a88f153a3b82c3a354dab2.r2.cloudflarestorage.com',
    region_name='auto', aws_access_key_id=Path('/tmp/r2-access-secure').read_text().strip(),
    aws_secret_access_key=Path('/tmp/r2-secret-secure').read_text().strip())
bucket = 'mowang-debs'
report = {'prefix': prefix, 'branch': branch, 'production_writes': 0,
          'production_gc_enabled': False, 'checks': {}, 'scope': 'engine contract, not full APT publisher E2E'}


def put(key, raw, owner=gc.TEST_OWNER, **kwargs):
    assert key.startswith(prefix)
    return client.put_object(Bucket=bucket, Key=key, Body=raw,
        Metadata={'owner': owner, 'owner-version': '1', 'sha256': gc.digest(raw), 'size': str(len(raw))},
        CacheControl='public, max-age=3600', **kwargs)


def absent(key):
    try:
        client.head_object(Bucket=bucket, Key=prefix + key)
    except ClientError as error:
        return error.response['ResponseMetadata']['HTTPStatusCode'] == 404
    return False


def publish(rows, number):
    # Write and authenticate distinct successful test receipts. Gate checks all
    # live bytes and our conditional lock, not arbitrary local receipt JSON.
    snapshot = {'objects': rows}
    raw = gc.encoded(snapshot)
    receipt = gc.encoded({'snapshot_sha256': gc.digest(raw), 'run': number, 'success': True})
    key = prefix + 'receipts/' + str(number) + '.json'
    put(key, receipt, IfNoneMatch='*')
    put(prefix + 'managed-manifest.json', raw)
    assert gc.owned_bytes(client, bucket, key, gc.TEST_OWNER) == receipt
    identity = gc.digest(receipt)
    def gate():
        assert gc.owned_bytes(client, bucket, prefix + 'production-publish.lock', gc.TEST_OWNER) == lock
        assert gc.owned_bytes(client, bucket, key, gc.TEST_OWNER) == receipt
        assert gc.owned_bytes(client, bucket, prefix + 'managed-manifest.json', gc.TEST_OWNER) == raw
        return identity
    return snapshot, identity, gate


lock = gc.encoded({'nonce': uuid.uuid4().hex, 'branch': branch})
put(prefix + 'production-publish.lock', lock, IfNoneMatch='*')
try:
    raw = b'isolated-engine-contract-payload-' + uuid.uuid4().hex.encode()
    key = 'debs/' + gc.digest(raw) + '.deb'
    put(prefix + key, raw, IfNoneMatch='*')
    row = {'sha256': gc.digest(raw), 'size': len(raw), 'refs': 2, 'ownership': 'managed'}
    snap, identity, gate = publish({key: row}, 1)
    state = gc.observe(None, snap, identity, 0)
    snap, identity, gate = publish({key: dict(row, refs=1)}, 2)
    state = gc.observe(state, snap, identity, 1)
    assert not gc.candidates(state, 2 * gc.GRACE) and not absent(key)
    report['checks']['two_records_delete_one_retains_shared_object'] = True
    snap, identity, gate = publish({}, 3)
    state = gc.observe(state, snap, identity, 2)
    assert not gc.candidates(state, 3 * gc.GRACE)
    report['checks']['one_absent_snapshot_not_enough'] = True
    snap, identity, gate = publish({}, 4)
    state = gc.observe(state, snap, identity, 2 + gc.GRACE)
    assert list(gc.candidates(state, 2 + gc.GRACE)) == [key]
    report['checks']['two_absent_snapshots_and_48h'] = True
    restored = gc.observe(state, {'objects': {key: row}}, 'a' * 64, 2 + gc.GRACE)
    assert not gc.candidates(restored, 3 * gc.GRACE)
    report['checks']['restore_cancels_candidate'] = True
    # Actual conditional DELETE rejection: wrong ETag must leave object intact.
    try:
        client.delete_object(Bucket=bucket, Key=prefix + key, IfMatch='"incorrect-etag"')
    except ClientError as error:
        report['checks']['r2_conditional_wrong_etag_status'] = error.response['ResponseMetadata']['HTTPStatusCode']
        assert report['checks']['r2_conditional_wrong_etag_status'] == 412
    else:
        raise AssertionError('R2 ignored IfMatch; unsafe API')
    assert not absent(key)
    # External owner with matching content is never adopted or deleted.
    put(prefix + key, raw, owner='external')
    result = gc.delete_isolated(client, bucket, prefix, gc.TEST_OWNER, state, 2 + gc.GRACE, gate)
    assert result['protected'] and not absent(key)
    report['checks']['real_foreign_owner_protected'] = True
    put(prefix + key, raw)
    try:
        gc.delete_isolated(client, bucket, prefix, gc.TEST_OWNER, state, 2 + gc.GRACE, lambda: 'b' * 64)
    except ValueError:
        assert not absent(key)
        report['checks']['new_head_rejected'] = True
    else:
        raise AssertionError('new head accepted')
    # Corrupt failed publication/live evidence blocks even a mature state.
    put(prefix + 'managed-manifest.json', b'failed-publication')
    try:
        gc.delete_isolated(client, bucket, prefix, gc.TEST_OWNER, state, 2 + gc.GRACE, gate)
    except AssertionError:
        assert not absent(key)
        report['checks']['failed_publication_retains_object'] = True
    else:
        raise AssertionError('failed publication accepted')
    put(prefix + 'managed-manifest.json', gc.encoded(snap))
    # Same-key replacement between HEAD and DELETE makes the captured ETag stale.
    class Race:
        def head_object(self, **kwargs):
            head = client.head_object(**kwargs)
            put(prefix + key, raw + b'changed')
            return head
        def delete_object(self, **kwargs):
            return client.delete_object(**kwargs)
    try:
        gc.delete_isolated(Race(), bucket, prefix, gc.TEST_OWNER, state, 2 + gc.GRACE, gate)
    except ClientError as error:
        assert error.response['ResponseMetadata']['HTTPStatusCode'] == 412 and not absent(key)
        report['checks']['real_same_key_replacement_race_412'] = True
    else:
        raise AssertionError('replacement race was not conditional')
    put(prefix + key, raw)
    # Warm public immutable cache before DELETE; audit public status separately.
    with urllib.request.urlopen(base + key, timeout=120) as response:
        assert response.read() == raw
        report['public_before'] = {'status': response.status, 'cache_control': response.headers.get('Cache-Control')}
    result = gc.delete_isolated(client, bucket, prefix, gc.TEST_OWNER, state, 2 + gc.GRACE, gate)
    assert result['deleted'] == [key] and absent(key)
    report['checks']['real_conditional_delete_s3_404'] = True
    for label, url in [('public_exact_after', base + key), ('public_cachebust_after', base + key + '?gc=' + uuid.uuid4().hex)]:
        try:
            with urllib.request.urlopen(url, timeout=120) as response:
                report[label] = {'status': response.status, 'cache_control': response.headers.get('Cache-Control'),
                                 'body_sha256': gc.digest(response.read())}
        except urllib.error.HTTPError as error:
            report[label] = {'status': error.code, 'cache_control': error.headers.get('Cache-Control')}
    report['success'] = True
finally:
    # Only own exact lock; keep receipts/manifests for audit. No recursive cleanup.
    h = client.head_object(Bucket=bucket, Key=prefix + 'production-publish.lock')
    client.delete_object(Bucket=bucket, Key=prefix + 'production-publish.lock', IfMatch=h['ETag'])
    Path('r2-gc-isolated-e2e.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2))
