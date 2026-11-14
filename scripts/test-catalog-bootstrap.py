#!/usr/bin/env python3
"""Bootstrap coverage/reconciliation tests; no network or real repository mutation."""
import contextlib
import copy
import hashlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import catalog as c

spec = importlib.util.spec_from_file_location('bootstrap', Path(__file__).with_name('bootstrap-catalog.py'))
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)
BASE = 'https://test.invalid/repo'
COMMIT = 'a' * 40
TREE = 'b' * 40
DATA = b'abc'
DIGEST = c.sha(DATA)
BLOB = hashlib.sha1(b'blob 3\x00' + DATA).hexdigest()


def paragraph(path):
    return (b'Package: org.example.one\nVersion: 1.0\nArchitecture: iphoneos-arm64\nFilename: ' + path +
            b'\nSize: 3\nSHA256: ' + DIGEST.encode() + b'\nDescription: legacy\xff')


class Process:
    def __init__(self): self.stdout = io.BytesIO(DATA)
    def wait(self): return 0
    def poll(self): return 0
    def kill(self): pass


class Bootstrap(unittest.TestCase):
    def setUp(self):
        self.paths = [b'debs/one.deb', b'debs/\xe4\xb8\xad\xe6\x96\x87 two.deb']
        self.original = b'\n\n'.join(paragraph(p) for p in self.paths) + b'\n\n'
        self.snapshot = {p: BLOB for p in self.paths}
        key = 'debs/' + DIGEST + '.deb'
        self.public = b'\n\n'.join(paragraph(p).replace(b'Filename: ' + p, b'Filename: ' + key.encode()) for p in self.paths) + b'\n\n'
        self.manifest = {'schema': 1, 'base': BASE, 'commit': COMMIT, 'physical_gc_enabled': False,
                         'files': {p[5:].decode(): key for p in self.paths},
                         'objects': {key: {'sha256': DIGEST, 'size': 3, 'refs': 2}}}

    def run_bootstrap(self, directory, prepare=False):
        def git(root, *args):
            if args == ('show', 'HEAD:Packages'): return self.original
            if args == ('rev-parse', 'HEAD'): return COMMIT.encode()
            if args == ('rev-parse', 'HEAD:debs'): return TREE.encode()
            raise AssertionError(args)
        def download(url):
            if url == BASE + '/Packages': return self.public
            if url == BASE + '/managed-manifest.json': return c.encoded(self.manifest)
            raise AssertionError(url)
        with patch.object(b, 'git', side_effect=git), patch.object(b, 'download', side_effect=download), \
             patch.object(c, 'tree_snapshot', return_value=self.snapshot), \
             patch.object(b.subprocess, 'Popen', side_effect=lambda *a, **k: Process()), \
             contextlib.redirect_stdout(io.StringIO()):
            b.bootstrap(Path('/unused'), Path(directory), BASE, prepare)

    def test_every_source_preserved_full_get_and_git_blob(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'out'
            self.run_bootstrap(out)
            records, ledger = c.load_catalog(out)
            self.assertEqual(len(records), 2)
            self.assertEqual(len(ledger['processed']), 2)
            proof = c.loads((out / 'catalog-state/bootstrap-proof.json').read_bytes())
            self.assertTrue(proof['git_blob_full_hash_verified'])
            self.assertTrue(proof['production_r2_get_reverification'])
            self.assertFalse(proof['production_ready'])
            self.assertEqual(proof['git_blobs_hashed'], 1)
            self.assertEqual(proof['source_bytes'], 6)
            self.assertEqual(proof['unique_bytes'], 3)

    def test_prepare_mode_explicitly_not_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'out'
            self.run_bootstrap(out, True)
            proof = c.loads((out / 'catalog-state/bootstrap-proof.json').read_bytes())
            self.assertEqual(proof['mode'], 'draft-preparation')
            self.assertFalse(proof['git_blob_full_hash_verified'])
            self.assertFalse(proof['production_r2_get_reverification'])
            self.assertEqual(proof['git_blobs_hashed'], 0)

    def rejects_without_output(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'out'
            with self.assertRaises(ValueError): self.run_bootstrap(out)
            self.assertFalse(out.exists())

    def test_metadata_drift_refused(self):
        self.public = self.public.replace(b'Version: 1.0', b'Version: 2.0', 1)
        self.rejects_without_output()

    def test_missing_git_source_refused(self):
        self.snapshot.pop(self.paths[0])
        self.rejects_without_output()

    def test_extra_git_source_refused(self):
        self.snapshot[b'debs/extra.deb'] = BLOB
        self.rejects_without_output()

    def test_wrong_original_git_blob_refused(self):
        self.snapshot = {p: 'c' * 40 for p in self.paths}
        self.rejects_without_output()

    def test_manifest_filename_mapping_refused(self):
        self.manifest['files'][self.paths[0][5:].decode()] = 'evil/key'
        self.rejects_without_output()

    def test_manifest_size_mismatch_refused(self):
        next(iter(self.manifest['objects'].values()))['size'] = 4
        self.rejects_without_output()

    def test_manifest_missing_ref_refused(self):
        next(iter(self.manifest['objects'].values()))['refs'] = 1
        self.rejects_without_output()

    def test_manifest_extra_object_refused(self):
        self.manifest['objects']['debs/' + 'f' * 64 + '.deb'] = {'sha256': 'f' * 64, 'size': 3, 'refs': 1}
        self.rejects_without_output()

    def test_nonempty_output_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'keep'
            marker.write_bytes(b'keep')
            with self.assertRaises(ValueError): self.run_bootstrap(directory)
            self.assertEqual(marker.read_bytes(), b'keep')


if __name__ == '__main__':
    unittest.main(verbosity=2)
