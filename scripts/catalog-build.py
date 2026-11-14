#!/usr/bin/env python3
"""Catalog-backed offline build adapter; legacy repositories retain old behavior."""
import argparse
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import catalog as c


def enabled(root):
    root = Path(root)
    if (root / 'packages').exists() or (root / 'catalog-state/processed.json').exists():
        return True
    return subprocess.run(['git', '-C', str(root), 'cat-file', '-e', 'HEAD:catalog-state/processed.json'],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def check(root):
    root = Path(root).resolve()
    records, ledger = c.load_catalog(root)
    # Only pending uploads require archive tools/local DEBs. Previously processed
    # sources are not truth: they may be removed, stale, or intentionally unlisted.
    snapshot = c.tree_snapshot(root)
    spec = importlib.util.spec_from_file_location('catalog_import', Path(__file__).with_name('import-catalog.py'))
    importer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(importer)
    todo = c.pending(snapshot, ledger)
    for path, blob in todo.items():
        with tempfile.TemporaryDirectory(prefix='catalog-check-') as directory:
            importer.archived_record(root, path, blob, Path(directory))
    # Also reject malformed uncommitted uploads in local usage, without requiring
    # any historical inbox files to remain checked out.
    changed = subprocess.check_output(['git', '-C', str(root), 'diff', '--name-only', '-z', 'HEAD', '--', 'debs/']).split(b'\0')
    extras = {os.fsencode(p.relative_to(root)) for p in (root / 'debs').glob('*.deb')
              if os.fsencode(p.relative_to(root)) not in snapshot}
    for path in extras | {p for p in changed if p.endswith(b'.deb')}:
        package = root / os.fsdecode(path)
        if not package.is_file():
            continue
        raw = package.read_bytes()
        blob = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
        token = c.source_token({'filename_b64': c.b64(path), 'git_blob': blob})
        if token in ledger['processed']:
            continue
        with tempfile.TemporaryDirectory(prefix='catalog-local-') as directory:
            destination = Path(directory) / os.fsdecode(path)
            destination.parent.mkdir(parents=True)
            destination.write_bytes(raw)
            control = subprocess.check_output(['dpkg-deb', '-I', str(destination), 'control'])
            if not control.endswith(b'\n'):
                raise ValueError('new upload control missing newline')
            subprocess.run(['dpkg-deb', '--fsys-tarfile', str(destination)], stdout=subprocess.DEVNULL, check=True)
            output = subprocess.check_output(['dpkg-scanpackages', '-m', 'debs'], cwd=directory)
            blocks = c.paragraphs(output)
            if len(blocks) != 1:
                raise ValueError('invalid local pending upload')
            record = c.make_record(importer.archive_metadata(blocks[0], raw), blob)
            if record['size'] != len(raw) or record['sha256'] != c.sha(raw):
                raise ValueError('local pending hash mismatch')
    return records, ledger


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['mode', 'check', 'render', 'validate'])
    parser.add_argument('--root', default='.')
    parser.add_argument('--output', default='Packages')
    args = parser.parse_args()
    root = Path(args.root)
    if args.action == 'mode':
        raise SystemExit(0 if enabled(root) else 1)
    if not enabled(root):
        raise ValueError('catalog mode required')
    records, ledger = check(root)
    rendered = c.render(records)
    if args.action == 'render':
        Path(args.output).write_bytes(rendered)
    elif args.action == 'validate' and Path(args.output).read_bytes() != rendered:
        raise ValueError('Packages differs from catalog source of truth')
    print('Catalog: %d active records / %d processed sources; inbox cleanup disabled' % (len(records), len(ledger['processed'])))


if __name__ == '__main__':
    main()
