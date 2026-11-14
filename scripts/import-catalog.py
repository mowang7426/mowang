#!/usr/bin/env python3
"""Archive-verified additive inbox import using a non-force GitHub ref CAS.

No inbox deletion and no R2 deletion. Every retry reads the latest committed tree;
processed tokens permanently prevent a deliberately unlisted record resurfacing.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import catalog as c


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args])


def archive_metadata(block, raw):
    """Derive checksums independently; dpkg supplies parsing, not hash trust.

    Some portable Perl Digest::SHA builds corrupt formatted digests. Recompute
    generated checksum fields from the exact Git bytes with Python, while
    preserving every control/description byte untouched.
    """
    import hashlib
    for field, algorithm in [('MD5sum', 'md5'), ('SHA1', 'sha1'), ('SHA256', 'sha256')]:
        block = re.sub(b'^' + field.encode() + rb': [^\n]*',
                       field.encode() + b': ' + hashlib.new(algorithm, raw).hexdigest().encode(),
                       block, flags=re.M)
    return block


def archived_record(root, path, blob, directory):
    """Read exactly the committed blob, never a mutable worktree file."""
    raw = git(root, 'cat-file', 'blob', blob)
    import hashlib
    if hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest() != blob:
        raise ValueError('Git blob mismatch')
    package = directory / os.fsdecode(path)
    package.parent.mkdir(parents=True, exist_ok=True)
    package.write_bytes(raw)
    control = subprocess.check_output(['dpkg-deb', '-I', str(package), 'control'])
    if not control.endswith(b'\n'):
        raise ValueError('control missing final newline')
    # Force decompression of BOTH archives. Neither command installs a package,
    # extracts files to the host, nor executes maintainer scripts.
    subprocess.run(['dpkg-deb', '--ctrl-tarfile', str(package)], stdout=subprocess.DEVNULL, check=True)
    subprocess.run(['dpkg-deb', '--fsys-tarfile', str(package)], stdout=subprocess.DEVNULL, check=True)
    subprocess.run(['dpkg-deb', '--contents', str(package)], stdout=subprocess.DEVNULL, check=True)
    output = subprocess.check_output(['dpkg-scanpackages', '-m', 'debs'], cwd=directory)
    blocks = c.paragraphs(output)
    if len(blocks) != 1:
        raise ValueError('expected exactly one archive')
    record = c.make_record(archive_metadata(blocks[0], raw), blob)
    if (c.unb64(record['source']['filename_b64']) != path or
            record['size'] != len(raw) or record['sha256'] != c.sha(raw)):
        raise ValueError('archive metadata/hash mismatch: ' + repr((path, c.unb64(record['source']['filename_b64']), len(raw), record['size'], c.sha(raw), record['sha256'])))
    if record['identity']['architecture'] not in {'iphoneos-arm', 'iphoneos-arm64', 'iphoneos-arm64e'}:
        raise ValueError('unsupported architecture')
    c.validate_record(record, record['id'] + '.json')
    return record, package


class GitHub:
    def __init__(self, repository, token):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
            raise ValueError('invalid repository')
        self.base = 'https://api.github.com/repos/' + repository
        self.token = token

    def request(self, method, path, value=None):
        data = json.dumps(value).encode() if value is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method,
            headers={'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json',
                     'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=90) as response:
            return json.load(response)

    def head(self, branch):
        return self.request('GET', '/git/ref/heads/' + urllib.parse.quote(branch, safe='/'))['object']['sha']

    def commit(self, branch, parent, changes):
        tree = self.request('GET', '/git/commits/' + parent)['tree']['sha']
        rows = []
        for path, data in changes.items():
            blob = self.request('POST', '/git/blobs', {'content': c.b64(data), 'encoding': 'base64'})['sha']
            rows.append({'path': path, 'mode': '100644', 'type': 'blob', 'sha': blob})
        updated = self.request('POST', '/git/trees', {'base_tree': tree, 'tree': rows})['sha']
        commit = self.request('POST', '/git/commits', {'message': 'catalog: import verified immutable inbox uploads',
            'tree': updated, 'parents': [parent]})['sha']
        try:
            # Single parent == observed head. A concurrent user commit is a
            # sibling, so GitHub rejects this non-fast-forward update (422).
            self.request('PATCH', '/git/refs/heads/' + urllib.parse.quote(branch, safe='/'),
                         {'sha': commit, 'force': False})
        except urllib.error.HTTPError as error:
            if error.code in (409, 422):
                return None
            raise
        return commit


def committed_catalog(root, commit):
    paths = git(root, 'ls-tree', '-rz', commit, '--', 'packages/', 'catalog-state/processed.json')
    records = []
    ledger = None
    for entry in paths.split(b'\0'):
        if not entry:
            continue
        header, rawpath = entry.split(b'\t', 1)
        mode, kind, blob = header.split()
        path = os.fsdecode(rawpath)
        if path != 'catalog-state/processed.json' and not path.endswith('.json'):
            continue
        if mode != b'100644' or kind != b'blob':
            raise ValueError('nonregular catalog entry')
        value = c.loads(git(root, 'cat-file', 'blob', blob.decode()))
        if path == 'catalog-state/processed.json':
            ledger = c.validate_ledger(value)
        else:
            if '/' in path[len('packages/'):]:
                raise ValueError('nested catalog JSON')
            records.append(c.validate_record(value, path[len('packages/'):]))
    if ledger is None or not records:
        raise ValueError('committed nonempty catalog required; bootstrap is separate')
    for record in records:
        if ledger['processed'].get(c.source_token(record['source'])) != c.ledger_row(record):
            raise ValueError('catalog/ledger mismatch')
    return records, ledger


def transaction(root, branch, api, upload, attempts=4):
    """upload(record,path) MUST verify immutable R2 HEAD/S3 GET/public GET."""
    uploaded = {}
    for attempt in range(attempts):
        parent = api.head(branch)
        # Fetch, never checkout/reset: preserve local and user work exactly.
        subprocess.run(['git', '-C', str(root), 'fetch', '--no-tags', 'origin', parent], check=True)
        records, ledger = committed_catalog(root, parent)
        inbox = c.tree_snapshot(root, parent)
        pending = c.pending(inbox, ledger)
        changes = {}
        for path, blob in sorted(pending.items()):
            token = c.source_token({'filename_b64': c.b64(path), 'git_blob': blob})
            if token not in uploaded:
                with tempfile.TemporaryDirectory(prefix='catalog-import-') as name:
                    record, package = archived_record(root, path, blob, Path(name))
                    evidence = upload(record, package)
                uploaded[token] = record, evidence
            record, evidence = uploaded[token]
            key = 'packages/' + record['id'] + '.json'
            if key in changes:
                raise ValueError('record collision')
            # All preexisting rows remain immutable; never recreate a processed
            # record whose JSON was deliberately deleted.
            changes[key] = c.encoded(record)
            ledger['processed'][token] = c.ledger_row(record)
        if not changes:
            if api.head(branch) == parent:
                return {'schema': 1, 'commit': parent, 'imported': 0, 'attempts': attempt + 1,
                        'cleanup_enabled': False}
            continue
        changes['catalog-state/processed.json'] = c.encoded(ledger)
        commit = api.commit(branch, parent, changes)
        if commit:
            return {'schema': 1, 'commit': commit, 'parent': parent,
                    'imported': len(changes) - 1, 'attempts': attempt + 1,
                    'cleanup_enabled': False}
    raise RuntimeError('import CAS retry cap exceeded; no force update and no inbox deletion')


def r2_upload():
    import boto3
    spec = importlib.util.spec_from_file_location('catalog_sync', Path(__file__).with_name('sync-r2.py'))
    sync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync)
    prefix = os.environ.get('R2_IMPORT_PREFIX', 'repo/').strip('/') + '/'
    if prefix != 'repo/' and not re.fullmatch(r'catalog-e2e/[A-Za-z0-9_-]+/', prefix):
        raise ValueError('unsafe isolated import prefix')
    base = os.environ['R2_PUBLIC_BASE_URL'].rstrip('/')
    if not base.endswith('/' + prefix.rstrip('/')):
        raise ValueError('public base/prefix mismatch')
    raw = boto3.client('s3', endpoint_url='https://' + os.environ['R2_ACCOUNT_ID'] + '.r2.cloudflarestorage.com', region_name='auto')
    class Scope:
        def __getattr__(self, name):
            def call(**kw):
                kw['Key'] = prefix + kw['Key']
                return getattr(raw, name)(**kw)
            return call
    owner = 'mowang7426/mowang-r2-production' if prefix == 'repo/' else 'mowang7426/mowang-catalog-e2e'
    def upload(record, package):
        return sync.sync_one(Scope(), os.environ['R2_BUCKET'], package, base, owner=owner)
    return upload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='.')
    parser.add_argument('--branch', default='main')
    parser.add_argument('--repository', default=os.environ.get('GITHUB_REPOSITORY', 'mowang7426/mowang'))
    parser.add_argument('--output', default='catalog-import-result.json')
    args = parser.parse_args()
    Path(args.output).unlink(missing_ok=True)
    result = transaction(Path(args.root), args.branch, GitHub(args.repository, os.environ['GITHUB_TOKEN']), r2_upload())
    Path(args.output).write_bytes(c.encoded(result))
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
