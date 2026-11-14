#!/usr/bin/env python3
"""Bootstrap from committed Packages, public R2 index/manifest and every Git blob.
No production write occurs. Output must be a fresh directory outside the repository.
"""
import argparse
import collections
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import catalog as c


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args])


def download(url):
    return subprocess.check_output(['curl', '--fail', '--silent', '--show-error',
                                    '--retry', '3', url])


def without_filename(block):
    return b'\n'.join(line for line in block.split(b'\n') if not line.startswith(b'Filename: '))


def bootstrap(root, output, base, prepare_draft=False, download_base=None):
    object_base = (download_base or base).rstrip('/')
    if output.exists():
        raise ValueError('output already exists; refuse overwrite')
    commit = git(root, 'rev-parse', 'HEAD').decode().strip()
    tree = git(root, 'rev-parse', 'HEAD:debs').decode().strip()
    original = git(root, 'show', 'HEAD:Packages')
    blocks = c.paragraphs(original)
    published = download(base + '/Packages')
    public_blocks = c.paragraphs(published)
    manifest_raw = download(base + '/managed-manifest.json')
    manifest = c.loads(manifest_raw)
    if manifest.get('schema') != 1 or manifest.get('base') != base or manifest.get('physical_gc_enabled') is not False:
        raise ValueError('invalid production manifest')
    if collections.Counter(map(without_filename, blocks)) != collections.Counter(map(without_filename, public_blocks)):
        raise ValueError('committed/public metadata differs')
    for block in public_blocks:
        f = c.fields(block)
        if f['Filename'] != b'debs/' + f['SHA256'] + b'.deb':
            raise ValueError('non-immutable public Filename')
    snapshot = c.tree_snapshot(root)
    if collections.Counter(c.fields(b)['Filename'] for b in blocks) != collections.Counter(snapshot.keys()):
        raise ValueError('source paragraph / Git tree coverage mismatch')
    records = []
    objects = {}
    for block in blocks:
        f = c.fields(block)
        path = f['Filename']
        digest = f['SHA256'].decode()
        size = int(f['Size'])
        key = 'debs/' + digest + '.deb'
        manifest_name = path[5:].decode('utf-8', 'surrogateescape')
        if manifest['files'].get(manifest_name) != key:
            raise ValueError('manifest filename mapping mismatch')
        row = manifest['objects'].get(key, {})
        if row.get('sha256') != digest or row.get('size') != size:
            raise ValueError('manifest digest/size mismatch')
        record = c.make_record(block, snapshot[path])
        records.append(record)
        expected = (digest, size, snapshot[path])
        if key in objects and objects[key] != expected:
            raise ValueError('same content has inconsistent Git blob or size')
        objects[key] = expected
    # Public full GET also reconstructs Git's SHA1("blob SIZE\\0" + bytes).
    # Equality with the trusted Git tree proves the original blob without fetching
    # 3.45 GB into a partial clone. DEBs are streamed and never written locally.
    failed = threading.Event()
    def verify(item):
        if failed.is_set():
            raise RuntimeError('another full-object verification failed; stop queued reads')
        key, (digest, size, blob) = item
        for attempt in range(3):
            try:
                process = subprocess.Popen(['curl', '--fail', '--silent', '--show-error',
                    '--max-time', '180', '--connect-timeout', '30', '--retry', '3',
                    '--retry-delay', '15', '--retry-max-time', '120', object_base + '/' + key], stdout=subprocess.PIPE)
                try:
                    h = c.hashlib.sha256()
                    g = c.hashlib.sha1(b'blob ' + str(size).encode() + b'\x00')
                    count = 0
                    while True:
                        chunk = process.stdout.read(1024 * 1024)
                        if not chunk:
                            break
                        count += len(chunk)
                        if count > size:
                            raise ValueError('oversized public object')
                        h.update(chunk)
                        g.update(chunk)
                    if process.wait() != 0 or count != size or h.hexdigest() != digest or g.hexdigest() != blob:
                        raise ValueError('public DEB hash / Git blob mismatch')
                    return key
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.stdout.close()
                    process.wait()
            except Exception:
                if attempt == 2:
                    failed.set()
                    raise
    verified = set()
    if prepare_draft:
        print('DRAFT PREPARATION ONLY: full object verification is NOT complete; publication forbidden', flush=True)
    else:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(verify, item) for item in objects.items()]
            for future in as_completed(futures):
                verified.add(future.result())
                if len(verified) % 100 == 0:
                    print('Full R2 GET and trusted Git blob verified', len(verified), flush=True)
    expected_objects = collections.Counter(r['object_key'] for r in records)
    if set(expected_objects) != set(manifest['objects']):
        raise ValueError('public manifest object coverage mismatch')
    for key, count in expected_objects.items():
        if manifest['objects'][key].get('refs') != count:
            raise ValueError('public manifest refcount mismatch')
    ledger = {'schema': 1, 'processed': {c.source_token(r['source']): c.ledger_row(r) for r in records}}
    if len(ledger['processed']) != len(records) or len({r['id'] for r in records}) != len(records):
        raise ValueError('source / record ID collision')
    output.mkdir(parents=True)
    (output / 'packages').mkdir()
    (output / 'catalog-state').mkdir()
    for record in records:
        c.validate_record(record, record['id'] + '.json')
        (output / 'packages' / (record['id'] + '.json')).write_bytes(c.encoded(record))
    (output / 'catalog-state' / 'processed.json').write_bytes(c.encoded(ledger))
    c.load_catalog(output)
    index = c.render(sorted(records, key=lambda r: r['id']))
    if collections.Counter(map(without_filename, c.paragraphs(index))) != collections.Counter(map(without_filename, blocks)):
        raise ValueError('generated index metadata differs')
    report = {'schema': 1, 'bootstrap_commit': commit, 'debs_tree': tree,
              'original_packages_sha256': c.sha(original), 'public_packages_sha256': c.sha(published),
              'manifest_sha256': c.sha(manifest_raw), 'public_manifest_commit': manifest['commit'],
              'records': len(records), 'unique_objects': len(expected_objects),
              'git_blobs_hashed': len(verified), 'source_bytes': sum(r['size'] for r in records),
              'unique_bytes': sum(v['size'] for v in manifest['objects'].values()),
              'raw_metadata_multiset_equal': True, 'git_blob_full_hash_verified': not prepare_draft,
              'production_r2_get_reverification': not prepare_draft,
              'production_ready': False, 'mode': 'draft-preparation' if prepare_draft else 'full-readonly-verification',
              'production_modified': False,
              'cleanup_enabled': False, 'physical_gc_enabled': False}
    (output / 'catalog-state' / 'bootstrap-proof.json').write_bytes(c.encoded(report))
    (output / 'catalog.Packages').write_bytes(index)
    (output / 'cleanup-plan.json').write_bytes(c.encoded(c.cleanup_plan(records, ledger, snapshot, commit, tree)))
    print(c.encoded(report).decode(), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='.')
    parser.add_argument('--output', required=True)
    parser.add_argument('--base', default='https://pub-f47a5a9b45724684be0f3f006e95e609.r2.dev/repo')
    parser.add_argument('--prepare-draft', action='store_true', help='Create explicitly unverified draft inventory only; NEVER a publication receipt')
    parser.add_argument('--download-base', help='Official read-only download frontend; does not change trusted manifest base')
    args = parser.parse_args()
    bootstrap(Path(args.root), Path(args.output), args.base.rstrip('/'), args.prepare_draft, args.download_base)
