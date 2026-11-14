#!/usr/bin/env python3
"""Real import CAS -> production publisher receipt -> shared reference -> DELETE.

Only catalog-e2e/r2-gc-* prefixes and their exact independent branch are accepted.
Production clock cannot be changed. Uses real S3 and curl, not mock transport.
"""
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import boto3
from botocore.exceptions import ClientError
import catalog as c
import r2_shared_lock as locks


def module(name, file):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(file))
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


imp = module('gc_import', 'import-catalog.py')
branch = os.environ['R2_TEST_BRANCH']
prefix = locks.scope()
assert re.fullmatch(r'catalog-e2e/r2-gc-[A-Za-z0-9_-]+', branch)
assert prefix == branch + '/'
api = imp.GitHub('mowang7426/mowang', os.environ['GITHUB_TOKEN'])
s3 = locks.client()
bucket = os.environ['R2_BUCKET']
base = os.environ['R2_PUBLIC_BASE_URL']
report = {'branch': branch, 'prefix': prefix, 'checks': {}, 'public': {},
          'production_writes': 0, 'production_deletes': 0, 'receipts': []}
now = time.time()
count = 0


def reload(commit):
    subprocess.run(['git', 'fetch', '--no-tags', 'origin', commit], check=True)
    subprocess.run(['git', 'checkout', '--detach', commit], check=True)


def commit(message, changes, load=True):
    parent = api.head(branch)
    tree = api.request('GET', '/git/commits/' + parent)['tree']['sha']
    rows = []
    for path, raw in changes.items():
        blob = api.request('POST', '/git/blobs', {'content': c.b64(raw), 'encoding': 'base64'})['sha'] if raw is not None else None
        rows.append({'path': path, 'mode': '100644', 'type': 'blob', 'sha': blob})
    tree = api.request('POST', '/git/trees', {'base_tree': tree, 'tree': rows})['sha']
    head = api.request('POST', '/git/commits', {'message': message, 'tree': tree, 'parents': [parent]})['sha']
    api.request('PATCH', '/git/refs/heads/' + branch, {'sha': head, 'force': False})
    if load:
        reload(head)
    return head


def state():
    return s3.get_object(Bucket=bucket, Key=prefix + 'physical-gc-state-v2.json')['Body'].read()


def present(key):
    try:
        s3.head_object(Bucket=bucket, Key=prefix + key)
        return True
    except ClientError as e:
        if e.response['ResponseMetadata']['HTTPStatusCode'] == 404:
            return False
        raise


def public(key):
    return subprocess.check_output(['curl', '-sS', '-A', 'Mozilla/5.0', '--max-time', '90',
        '-o', '/tmp/r2-gc-e2e-public', '-w', '%{http_code}', base + '/' + key], text=True)


def publish(label):
    global count
    count += 1
    os.environ['GITHUB_RUN_ATTEMPT'] = str(count)
    os.environ['R2_GC_TEST_NOW'] = str(now)
    for cmd in (['bash', 'build.sh'], ['bash', 'validate-repo.sh'],
                ['bash', 'scripts/update-repository.sh'],
                [sys.executable, 'scripts/publish-r2.py']):
        subprocess.run(cmd, check=True)
    result = c.loads(Path('r2-physical-gc-result.json').read_bytes())
    receipt = c.loads(Path('r2-publication-receipt.json').read_bytes())
    assert receipt['success']
    report['receipts'].append({'label': label, 'commit': receipt['commit'],
        'receipt_sha256': result['receipt_sha256'], 'result': result})
    Path('e2e-gc-' + label + '.json').write_bytes(c.encoded(result))
    return result


# Use the real importer main (archive validation + lock + immutable claim + Git CAS).
subprocess.run([sys.executable, 'scripts/import-catalog.py', '--branch', branch], check=True)
imported = c.loads(Path('catalog-import-result.json').read_bytes())
assert imported['imported'] == 1
reload(imported['commit'])
rs, ledger = c.load_catalog('.')
pending = next(r for r in rs if c.unb64(r['source']['filename_b64']) == b'debs/pending.deb')
raw = c.unb64(pending['metadata_b64'])
alias = c.make_record(re.sub(rb'^Filename: [^\n]*', b'Filename: debs/shared-alias.deb', raw, flags=re.M), pending['source']['git_blob'])
ledger['processed'][c.source_token(alias['source'])] = c.ledger_row(alias)
commit('test: two managed records share one immutable object', {
    'packages/' + alias['id'] + '.json': c.encoded(alias),
    'catalog-state/processed.json': c.encoded(ledger)})
result = publish('baseline')
assert result['baseline'] and not result['pending']
manifest = c.loads(s3.get_object(Bucket=bucket, Key=prefix + 'managed-manifest.json')['Body'].read())
key = pending['object_key']
assert manifest['objects'][key]['refs'] == 2 and manifest['objects'][key]['ownership'] == 'managed'
report['checks']['new_import_creation_claim_managed'] = True
report['public']['before'] = public(key)
assert report['public']['before'] == '200'

# A competing publisher/importer cannot enter while the shared lock is held.
with locks.hold() as held:
    env = dict(os.environ)
    env.pop('R2_SHARED_LOCK_TOKEN', None)
    competing = subprocess.run([sys.executable, 'scripts/import-catalog.py', '--branch', branch], env=env, capture_output=True)
    assert competing.returncode != 0 and b'PreconditionFailed' in competing.stderr
    held.verify()
    assert present(key)
report['checks']['competing_importer_refused_under_shared_mutex'] = True

commit('test: remove first shared reference', {'packages/' + pending['id'] + '.json': None})
now += 1
result = publish('one-shared-reference')
assert result['pending'] == 0 and present(key)
report['checks']['one_shared_reference_retains'] = True

# Commit a last-ref deletion but intentionally publish stale checkout: must fail.
before = state()
head = commit('test: remove last shared reference', {'packages/' + alias['id'] + '.json': None}, load=False)
r = subprocess.run([sys.executable, 'scripts/publish-r2.py'], capture_output=True)
assert r.returncode != 0 and state() == before and present(key)
report['checks']['stale_publish_does_not_advance_state'] = True
reload(head)
now += 1
result = publish('last-reference')
assert result['pending'] == 1 and present(key)

# Restore reference even after time passes: publish verifies S3 and cancels event.
commit('test: restore reference before physical collection', {'packages/' + alias['id'] + '.json': c.encoded(alias)})
now += 172801
result = publish('restored')
assert result['pending'] == 0 and present(key)
report['checks']['restored_reference_cancels'] = True
commit('test: final unlist after restore', {'packages/' + alias['id'] + '.json': None})
now += 1
result = publish('new-removal')
assert result['pending'] == 1 and present(key)
now += 172799
result = publish('before-grace')
assert not result['deleted'] and present(key)
now += 1
result = publish('mature-delete')
assert result['deleted'] == [key] and not present(key)
report['checks']['last_reference_48h_two_receipts_real_delete_head404'] = True
report['deleted_key'] = prefix + key
report['public']['after'] = public(key)
assert report['public']['after'] == '404', report
report['checks']['public_curl_404_after_real_delete'] = True

# JSON alone cannot resurrect an already deleted archive: fail before Release.
commit('test: JSON-only restore requires original deb', {'packages/' + alias['id'] + '.json': c.encoded(alias)})
before = state()
release = s3.get_object(Bucket=bucket, Key=prefix + 'Release')['Body'].read()
for cmd in (['bash', 'build.sh'], ['bash', 'validate-repo.sh'], ['bash', 'scripts/update-repository.sh']):
    subprocess.run(cmd, check=True)
r = subprocess.run([sys.executable, 'scripts/publish-r2.py'], capture_output=True)
assert r.returncode != 0 and b'HeadObject' in r.stderr and state() == before
assert s3.get_object(Bucket=bucket, Key=prefix + 'Release')['Body'].read() == release
report['checks']['json_only_restore_missing_deb_refused'] = True
report['success'] = True
report['code_head'] = os.environ.get('R2_GC_CODE_HEAD')
Path('e2e-gc-final.json').write_bytes(c.encoded(report))
print('REAL GC E2E SUCCESS ' + json.dumps(report, sort_keys=True))
