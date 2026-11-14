#!/usr/bin/env python3
"""Catalog publication evidence. No uploads, Git mutations or object deletion.

Publication reads permanent committed records, never the inbox. Every currently
listed content object is HEAD + authenticated full GET + public full GET checked.
"""
import hashlib
import importlib.util
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SPEC = importlib.util.spec_from_file_location('catalog', Path(__file__).with_name('catalog.py'))
c = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(c)
PRODUCTION_BASE = 'https://pub-f47a5a9b45724684be0f3f006e95e609.r2.dev/repo'
PUBLIC_READ_BASE = 'https://mowang-repo.pages.dev'


def read_base(base):
    # Index filenames remain R2-relative; the read-only Pages R2 binding serves
    # identical bytes without the shared r2.dev hostname's rate limiting.
    return PUBLIC_READ_BASE if base == PRODUCTION_BASE else base


def snapshot(root, commit):
    """Bind local JSON bytes to a committed tree; reject pending upload bypass."""
    root = Path(root)
    if not re.fullmatch(c.BLOB, commit):
        raise ValueError('invalid catalog commit')
    records, ledger = c.load_catalog(root)
    raw = subprocess.check_output(['git', '-C', str(root), 'ls-tree', '-rz', commit,
                                   '--', 'packages/', 'catalog-state/processed.json'])
    expected = {'packages/' + r['id'] + '.json' for r in records}
    expected.add('catalog-state/processed.json')
    found = set()
    for entry in raw.split(b'\x00'):
        if not entry:
            continue
        header, path = entry.split(b'\t', 1)
        name = path.decode('ascii')
        if not (name == 'catalog-state/processed.json' or name.endswith('.json')):
            continue
        mode, kind, blob = header.split()
        if mode != b'100644' or kind != b'blob' or name not in expected:
            raise ValueError('catalog record set differs from committed tree')
        data = (root / name).read_bytes()
        actual = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\x00' + data).hexdigest()
        if actual != blob.decode():
            raise ValueError('uncommitted catalog or ledger bytes')
        found.add(name)
    if found != expected:
        raise ValueError('untracked or missing catalog records')
    if c.pending(c.tree_snapshot(root, commit), ledger):
        raise ValueError('pending inbox uploads must be imported and CAS-committed before publication')
    return records, ledger


def catalog_objects(records):
    objects, files, blobs = {}, {}, {}
    for record in records:
        c.validate_record(record, record['id'] + '.json')
        key = record['object_key']
        row = objects.setdefault(key, {'sha256': record['sha256'], 'size': record['size'], 'refs': 0})
        if (row['sha256'], row['size']) != (record['sha256'], record['size']):
            raise ValueError('conflicting catalog object')
        row['refs'] += 1
        # files is a record->object mapping, not a filesystem/inbox projection.
        files[record['id'] + '.json'] = key
        blobs.setdefault(key, set()).add(record['source']['git_blob'])
    if not objects:
        raise ValueError('empty catalog requires independent all-unlist approval')
    if any(len(value) != 1 for value in blobs.values()):
        raise ValueError('same content cannot correspond to different Git blobs')
    return objects, files, blobs


def digest(stream, size):
    h = hashlib.sha256()
    g = hashlib.sha1(b'blob ' + str(size).encode() + b'\x00')
    count = 0
    while True:
        block = stream.read(1024 * 1024)
        if not block:
            return count, h.hexdigest(), g.hexdigest()
        count += len(block)
        if count > size:
            raise ValueError('object larger than catalog size')
        h.update(block)
        g.update(block)


def public_digest(base, key, size):
    # key was reconstructed by strict catalog validation; never accept a record URL.
    if not re.fullmatch(r'debs/[0-9a-f]{64}\.deb', key):
        raise ValueError('unsafe public object key')
    with tempfile.TemporaryFile() as output:
        subprocess.run(['curl', '--fail', '--silent', '--show-error', '--location',
                        '--max-redirs', '0', '--connect-timeout', '30', '--max-time', '1800',
                        '--retry', '5', '--retry-max-time', '600', '--retry-all-errors',
                        read_base(base).rstrip('/') + '/' + key], stdout=output, check=True)
        output.seek(0)
        return digest(output, size)


def verify_objects(s3, bucket, base, records, owner, previous=None, gate=lambda: None):
    """All evidence comes from live HEAD/GETs, not from bootstrap or prior receipt."""
    objects, files, blobs = catalog_objects(records)
    previous_objects = previous['objects'] if previous else {}

    gate()

    def one(item):
        key, row = item
        head = s3.head_object(Bucket=bucket, Key=key)
        metadata = head.get('Metadata', {})
        required = {'sha256': row['sha256'], 'size': str(row['size']),
                    'owner': owner, 'owner-version': '1'}
        if head.get('ContentLength') != row['size'] or any(metadata.get(k) != v for k, v in required.items()):
            raise ValueError('catalog object HEAD metadata/ownership/size mismatch: ' + key)
        expected = (row['size'], row['sha256'], next(iter(blobs[key])))
        response = s3.get_object(Bucket=bucket, Key=key)
        try:
            if digest(response['Body'], row['size']) != expected:
                raise ValueError('catalog S3 GET digest/blob mismatch: ' + key)
        finally:
            response['Body'].close()
        if public_digest(base, key, row['size']) != expected:
            raise ValueError('catalog public GET digest/blob mismatch: ' + key)
        # Strictly retain prior ownership; an owner stamp alone never adopts an
        # object not previously tracked as managed. Physical GC remains disabled.
        ownership = 'managed' if previous_objects.get(key, {}).get('ownership') == 'managed' else 'external'
        evidence = {'sha256': row['sha256'], 'size': row['size'],
                    'head_metadata_verified': True, 'full_get_hash_verified': True}
        details = {'owner': owner, 'owner_version': '1', 'etag': head.get('ETag'),
                   'git_blob': expected[2], 's3_full_get_verified': True,
                   'public_full_get_verified': True}
        return key, ownership, evidence, details

    verified, attestations = {}, {}
    # Deliberately bounded to two threads. R2's public hostname rejects broad fanout.
    with ThreadPoolExecutor(max_workers=2) as pool:
        for key, ownership, evidence, details in pool.map(one, sorted(objects.items())):
            objects[key]['ownership'] = ownership
            verified[key] = evidence
            attestations[key] = details
    gate()
    return objects, files, verified, attestations


def receipt(records, commit, site, objects, verified, attestations, base, run_id, manifest_bytes):
    ordered = sorted(records, key=lambda r: r['id'])
    if (Path(site) / 'Packages').read_bytes() != c.render(ordered):
        raise ValueError('published index is not the exact committed catalog rendering')
    expected, _, _ = catalog_objects(records)
    if set(objects) != set(expected) or set(verified) != set(expected) or set(attestations) != set(expected):
        raise ValueError('incomplete receipt coverage')
    for key, row in expected.items():
        if verified[key] != {'sha256': row['sha256'], 'size': row['size'],
                             'head_metadata_verified': True, 'full_get_hash_verified': True}:
            raise ValueError('invalid object evidence')
    return {'schema': 1, 'success': True, 'commit': commit, 'run_id': run_id,
            'base': base, 'catalog_sha256': c.sha(c.encoded(ordered)),
            'index_sha256': c.sha(c.render(ordered)),
            'release_sha256': c.sha((Path(site) / 'Release').read_bytes()),
            'manifest_sha256': c.sha(manifest_bytes), 'records': len(records),
            'verified_objects': verified, 'object_attestations': attestations,
            'cleanup_enabled': False, 'physical_gc_enabled': False}
