#!/usr/bin/env python3
"""Offline catalog foundation. No Git writes, R2 writes, cleanup or publication.

Permanent records plus an append-only processed-source ledger replace inbox-as-truth.
All metadata remains byte-exact except Filename in the generated index.
"""
import argparse
import base64
import collections
import hashlib
import json
import re
import subprocess
from pathlib import Path

HEX64 = r'[0-9a-f]{64}'
BLOB = r'[0-9a-f]{40}'
RECORD_FIELDS = {'schema', 'id', 'identity', 'sha256', 'size', 'object_key',
                 'metadata_b64', 'metadata_sha256', 'source'}
SOURCE_FIELDS = {'filename_b64', 'git_blob'}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def b64(data):
    return base64.b64encode(data).decode('ascii')


def unb64(value):
    if not isinstance(value, str):
        raise ValueError('base64 must be text')
    raw = base64.b64decode(value, validate=True)
    if b64(raw) != value:
        raise ValueError('noncanonical base64')
    return raw


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def loads(raw):
    return json.loads(raw, object_pairs_hook=unique_pairs)


def encoded(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + '\n').encode()


def fields(block):
    if not block or b'\x00' in block or b'\r' in block or b'\n\n' in block:
        raise ValueError('invalid control paragraph')
    result = {}
    previous = None
    for line in block.split(b'\n'):
        if line.startswith((b' ', b'\t')):
            if previous is None:
                raise ValueError('orphan continuation')
            if previous in ('Package', 'Version', 'Architecture', 'SHA256', 'Size', 'Filename'):
                raise ValueError('continued structural field')
            continue
        match = re.fullmatch(rb'([A-Za-z0-9][A-Za-z0-9-]*): ([^\n]*)', line)
        if not match:
            raise ValueError('invalid control field')
        key = match[1].decode('ascii')
        if key.lower() in {x.lower() for x in result}:
            raise ValueError('duplicate control field')
        result[key] = match[2]
        previous = key
    for key in ('Package', 'Version', 'Architecture', 'SHA256', 'Size', 'Filename'):
        if not result.get(key):
            raise ValueError('missing ' + key)
    for key in ('Package', 'Version', 'Architecture'):
        if len(result[key]) > 1024 or any(x < 32 or x == 127 for x in result[key]):
            raise ValueError('invalid identity')
    if not re.fullmatch(HEX64.encode(), result['SHA256']):
        raise ValueError('invalid SHA256')
    if not re.fullmatch(rb'[1-9][0-9]*', result['Size']):
        raise ValueError('invalid Size')
    filename(result['Filename'])
    return result


def filename(raw):
    if not raw.startswith(b'debs/') or not raw.endswith(b'.deb'):
        raise ValueError('not an inbox DEB path')
    part = raw[5:]
    if not part or b'/' in part or b'\\' in part or any(x < 32 or x == 127 for x in part):
        raise ValueError('unsafe inbox path')
    return raw


def paragraphs(raw):
    if not raw or not raw.endswith(b'\n\n'):
        raise ValueError('empty or unterminated index')
    blocks = raw[:-2].split(b'\n\n')
    for block in blocks:
        fields(block)
    return blocks


def identity(f):
    return {k.lower(): f[k].decode('utf-8', 'surrogateescape')
            for k in ('Package', 'Version', 'Architecture')}


def record_id(f, blob):
    def slug(raw, limit):
        return re.sub('[^a-z0-9.+-]+', '-', raw.decode('ascii', 'ignore').lower()).strip('-')[:limit] or 'legacy'
    stem = '--'.join(slug(f[k], n) for k, n in
                     [('Package', 42), ('Version', 22), ('Architecture', 22)])
    source = sha(f['Filename'] + b'\x00' + blob.encode())[:16]
    return stem + '--' + f['SHA256'].decode()[:16] + '--' + source


def make_record(block, blob):
    f = fields(block)
    if not re.fullmatch(BLOB, blob):
        raise ValueError('invalid Git blob')
    return {'schema': 1, 'id': record_id(f, blob), 'identity': identity(f),
            'sha256': f['SHA256'].decode(), 'size': int(f['Size']),
            'object_key': 'debs/' + f['SHA256'].decode() + '.deb',
            'metadata_b64': b64(block), 'metadata_sha256': sha(block),
            'source': {'filename_b64': b64(f['Filename']), 'git_blob': blob}}


def validate_record(record, name):
    if not isinstance(record, dict) or set(record) != RECORD_FIELDS or type(record['schema']) is not int or record['schema'] != 1:
        raise ValueError('record schema mismatch')
    source = record['source']
    if not isinstance(source, dict) or set(source) != SOURCE_FIELDS:
        raise ValueError('source schema mismatch')
    raw = unb64(record['metadata_b64'])
    rebuilt = make_record(raw, source['git_blob'])
    if type(record['size']) is not int or record['size'] <= 0 or record != rebuilt:
        raise ValueError('metadata / source / size / key mismatch')
    if name != record['id'] + '.json' or '/' in name or len(name.encode()) > 200:
        raise ValueError('record filename mismatch')
    return record


def source_token(source):
    filename(unb64(source['filename_b64']))
    if not re.fullmatch(BLOB, source['git_blob']):
        raise ValueError('bad source blob')
    return sha(unb64(source['filename_b64']) + b'\x00' + source['git_blob'].encode())


def ledger_row(record):
    return {'record': record['id'] + '.json', 'source': record['source'],
            'sha256': record['sha256'], 'size': record['size']}


def validate_ledger(ledger):
    if not isinstance(ledger, dict) or set(ledger) != {'schema', 'processed'} or type(ledger['schema']) is not int or ledger['schema'] != 1:
        raise ValueError('ledger schema mismatch')
    if not isinstance(ledger['processed'], dict) or not ledger['processed']:
        raise ValueError('empty ledger')
    for token, row in ledger['processed'].items():
        if not isinstance(row, dict) or set(row) != {'record', 'source', 'sha256', 'size'} or set(row['source']) != SOURCE_FIELDS:
            raise ValueError('invalid ledger row')
        if token != source_token(row['source']) or not re.fullmatch(HEX64, row['sha256']) or type(row['size']) is not int or row['size'] <= 0:
            raise ValueError('invalid ledger source')
        if not re.fullmatch(r'[a-z0-9.+-]{1,180}\.json', row['record']):
            raise ValueError('invalid ledger record path')
    return ledger


def load_catalog(root):
    root = Path(root)
    ledger = validate_ledger(loads((root / 'catalog-state' / 'processed.json').read_bytes()))
    records = []
    names = set()
    for path in sorted((root / 'packages').glob('*.json')):
        if path.is_symlink():
            raise ValueError('symlink record')
        record = validate_record(loads(path.read_bytes()), path.name)
        token = source_token(record['source'])
        if ledger['processed'].get(token) != ledger_row(record) or path.name in names:
            raise ValueError('record is not committed to processed ledger')
        names.add(path.name)
        records.append(record)
    if not records:
        raise ValueError('empty catalog requires a future separately reviewed all-unlist approval; not implemented')
    return records, ledger


def render(records):
    if not records:
        raise ValueError('empty catalog')
    blocks = []
    for record in records:
        validate_record(record, record['id'] + '.json')
        raw = unb64(record['metadata_b64'])
        blocks.append(re.sub(rb'^Filename: [^\n]*', b'Filename: ' + record['object_key'].encode(), raw, flags=re.M))
    return b'\n\n'.join(blocks) + b'\n\n'


def tree_snapshot(root, ref='HEAD'):
    raw = subprocess.check_output(['git', '-C', str(root), 'ls-tree', '-rz', ref, '--', 'debs/'])
    result = {}
    for entry in raw.split(b'\x00'):
        if not entry:
            continue
        head, path = entry.split(b'\t', 1)
        # Documentation and .gitkeep are not upload candidates; never clean them.
        if not path.endswith(b'.deb'):
            continue
        mode, kind, blob = head.split()
        if mode != b'100644' or kind != b'blob':
            raise ValueError('unsupported inbox entry')
        filename(path)
        if path in result:
            raise ValueError('duplicate Git path')
        result[path] = blob.decode()
    return result


def pending(snapshot, ledger):
    validate_ledger(ledger)
    return {path: blob for path, blob in snapshot.items()
            if source_token({'filename_b64': b64(path), 'git_blob': blob}) not in ledger['processed']}


def cleanup_plan(records, ledger, snapshot, commit, debs_tree):
    # Advisory only. Missing records are intentional unlists and NOT cleanup candidates.
    validate_ledger(ledger)
    rows = []
    for record in records:
        validate_record(record, record['id'] + '.json')
        src = record['source']
        if ledger['processed'].get(source_token(src)) != ledger_row(record):
            raise ValueError('record/ledger mismatch')
        if snapshot.get(unb64(src['filename_b64'])) == src['git_blob']:
            rows.append(ledger_row(record))
    return {'schema': 1, 'advisory_only': True, 'enabled': False,
            'base_commit': commit, 'debs_tree': debs_tree,
            'files': sorted(rows, key=lambda r: r['record']),
            'count': len(rows), 'bytes': sum(r['size'] for r in rows),
            'requires': ['separate user confirmation of exact list', 'successful publication receipt',
                         'full R2 GET hash', 'trusted main CAS', 'current exact blob',
                         'catalog still contains same record and hash']}


def command(args):
    root = Path(args.root)
    records, ledger = load_catalog(root)
    if args.action == 'validate':
        print(json.dumps({'records': len(records), 'processed_sources': len(ledger['processed']),
                          'unique_objects': len({r['sha256'] for r in records}),
                          'pending': len(pending(tree_snapshot(root), ledger))}, sort_keys=True))
    elif args.action == 'render':
        Path(args.output).write_bytes(render(records))
    else:
        git = lambda *a: subprocess.check_output(['git', '-C', str(root), *a], text=True).strip()
        plan = cleanup_plan(records, ledger, tree_snapshot(root), git('rev-parse', 'HEAD'), git('rev-parse', 'HEAD:debs'))
        Path(args.output).write_bytes(encoded(plan))
        print(json.dumps({k: v for k, v in plan.items() if k != 'files'}, sort_keys=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['validate', 'render', 'plan'])
    parser.add_argument('--root', default='.')
    parser.add_argument('--output')
    args = parser.parse_args()
    if args.action != 'validate' and not args.output:
        parser.error('--output required')
    command(args)
