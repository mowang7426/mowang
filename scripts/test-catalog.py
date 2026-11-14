#!/usr/bin/env python3
"""Offline catalog regressions. All repositories and inbox mutations are temporary."""
import copy
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
import catalog as c

spec = importlib.util.spec_from_file_location('gates', Path(__file__).with_name('catalog-gates.py'))
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)
A = 'a' * 40
B = 'b' * 40
COMMIT = 'c' * 40
TREE = 'd' * 40
DIGEST = 'e' * 64


def block(package=b'org.example.one', version=b'1.0', arch=b'iphoneos-arm64',
          path=b'debs/test.deb', digest=DIGEST, extra=b'Description: legacy\xff\n continuation'):
    return (b'Package: ' + package + b'\nVersion: ' + version + b'\nArchitecture: ' + arch +
            b'\nFilename: ' + path + b'\nSize: 3\nSHA256: ' + digest.encode() + b'\n' + extra)


def fixtures():
    records = [c.make_record(block(), A),
               c.make_record(block(package=b'org.example.two', path=b'debs/two.deb'), A)]
    ledger = {'schema': 1, 'processed': {c.source_token(r['source']): c.ledger_row(r) for r in records}}
    snapshot = {c.unb64(r['source']['filename_b64']): r['source']['git_blob'] for r in records}
    return records, ledger, snapshot


class Catalog(unittest.TestCase):
    def setUp(self):
        self.records, self.ledger, self.snapshot = fixtures()
        self.record = self.records[0]

    def test_raw_legacy_utf8_roundtrip(self):
        self.assertEqual(c.unb64(self.record['metadata_b64']), block())

    def test_rewrite_only_filename_bytes(self):
        expected = block().replace(b'Filename: debs/test.deb', b'Filename: debs/' + DIGEST.encode() + b'.deb') + b'\n\n'
        self.assertEqual(c.render([self.record]), expected)

    def test_unicode_original_filename_roundtrip(self):
        raw = block(path='debs/中文 空格.deb'.encode())
        record = c.make_record(raw, A)
        self.assertEqual(c.unb64(record['source']['filename_b64']), 'debs/中文 空格.deb'.encode())
        c.validate_record(record, record['id'] + '.json')

    def test_very_long_unicode_identity_short_ascii_path(self):
        record = c.make_record(block(package=('包' * 300).encode(), version=b'9' * 500), A)
        self.assertLess(len(record['id'] + '.json'), 200)
        (record['id'] + '.json').encode('ascii')

    def test_two_references_one_content_preserved(self):
        self.assertEqual(len(c.paragraphs(c.render(self.records))), 2)
        self.assertEqual(len({r['object_key'] for r in self.records}), 1)

    def test_same_identity_different_hash_preserved(self):
        other = c.make_record(block(digest='f' * 64), B)
        self.assertNotEqual(self.record['id'], other['id'])
        self.assertEqual(len(c.paragraphs(c.render([self.record, other]))), 2)

    def test_same_identity_same_hash_different_filenames_preserved(self):
        other = c.make_record(block(path=b'debs/copy.deb'), A)
        self.assertNotEqual(self.record['id'], other['id'])

    def test_versions_preserved(self):
        other = c.make_record(block(version=b'2.0'), B)
        self.assertNotEqual(self.record['id'], other['id'])

    def test_architectures_preserved(self):
        other = c.make_record(block(arch=b'iphoneos-arm64e'), B)
        self.assertNotEqual(self.record['id'], other['id'])

    def test_duplicate_control_refused(self):
        with self.assertRaises(ValueError): c.fields(block(extra=b'Package: evil'))

    def test_case_insensitive_duplicate_refused(self):
        with self.assertRaises(ValueError): c.fields(block(extra=b'package: evil'))

    def test_orphan_continuation_refused(self):
        with self.assertRaises(ValueError): c.fields(b' orphan\n' + block())

    def test_structural_continuation_refused(self):
        with self.assertRaises(ValueError): c.fields(block(extra=b' continuation'))

    def test_missing_field_refused(self):
        with self.assertRaises(ValueError): c.fields(block().replace(b'Version: 1.0\n', b''))

    def test_zero_size_refused(self):
        with self.assertRaises(ValueError): c.fields(block().replace(b'Size: 3', b'Size: 0'))

    def test_nul_metadata_refused(self):
        with self.assertRaises(ValueError): c.fields(block(extra=b'Description: bad\x00'))

    def test_url_filename_refused(self):
        with self.assertRaises(ValueError): c.make_record(block(path=b'https://evil/a.deb'), A)

    def test_traversal_refused(self):
        with self.assertRaises(ValueError): c.make_record(block(path=b'debs/../../evil.deb'), A)

    def test_nested_filename_refused(self):
        with self.assertRaises(ValueError): c.make_record(block(path=b'debs/sub/a.deb'), A)

    def test_backslash_refused(self):
        with self.assertRaises(ValueError): c.make_record(block(path=b'debs/..\\a.deb'), A)

    def test_duplicate_json_refused(self):
        with self.assertRaises(ValueError): c.loads('{"schema":1,"schema":1}')

    def test_noncanonical_base64_refused(self):
        with self.assertRaises(ValueError): c.unb64('YQ==\n')

    def test_extra_record_field_refused(self):
        record = dict(self.record, url='https://evil')
        with self.assertRaises(ValueError): c.validate_record(record, record['id'] + '.json')

    def test_arbitrary_object_key_refused(self):
        record = dict(self.record, object_key='some/other/bucket')
        with self.assertRaises(ValueError): c.validate_record(record, record['id'] + '.json')

    def test_wrong_size_refused(self):
        record = dict(self.record, size=4)
        with self.assertRaises(ValueError): c.validate_record(record, record['id'] + '.json')

    def test_boolean_size_refused(self):
        record = dict(self.record, size=True)
        with self.assertRaises(ValueError): c.validate_record(record, record['id'] + '.json')

    def test_wrong_metadata_hash_refused(self):
        record = dict(self.record, metadata_sha256='f' * 64)
        with self.assertRaises(ValueError): c.validate_record(record, record['id'] + '.json')

    def test_record_rename_refused(self):
        with self.assertRaises(ValueError): c.validate_record(self.record, 'other.json')

    def test_stale_inbox_does_not_resurrect_deleted_record(self):
        self.assertEqual(c.pending(self.snapshot, self.ledger), {})
        g.transition(self.records, self.ledger, self.records[1:], self.ledger)
        self.assertEqual(c.pending(self.snapshot, self.ledger), {})

    def test_new_blob_same_filename_is_pending(self):
        snapshot = dict(self.snapshot)
        snapshot[b'debs/test.deb'] = B
        self.assertEqual(c.pending(snapshot, self.ledger), {b'debs/test.deb': B})

    def test_empty_inbox_keeps_full_catalog(self):
        self.assertEqual(c.pending({}, self.ledger), {})
        self.assertEqual(len(c.paragraphs(c.render(self.records))), 2)

    def test_empty_catalog_fails_closed(self):
        with self.assertRaises(ValueError): c.render([])
        with self.assertRaises(ValueError): g.transition(self.records, self.ledger, [], self.ledger)

    def test_ledger_deletion_refused(self):
        ledger = copy.deepcopy(self.ledger)
        ledger['processed'].pop(next(iter(ledger['processed'])))
        with self.assertRaises(ValueError): g.transition(self.records, self.ledger, self.records[1:], ledger)

    def test_ledger_mutation_refused(self):
        ledger = copy.deepcopy(self.ledger)
        next(iter(ledger['processed'].values()))['size'] = 4
        with self.assertRaises(ValueError): g.transition(self.records, self.ledger, self.records, ledger)

    def test_readding_tombstoned_source_refused(self):
        with self.assertRaises(ValueError): g.transition(self.records[1:], self.ledger, self.records, self.ledger)

    def test_unlisted_record_not_cleanup_candidate(self):
        plan = c.cleanup_plan(self.records[1:], self.ledger, self.snapshot, COMMIT, TREE)
        self.assertEqual(plan['count'], 1)
        self.assertEqual(plan['files'][0]['source']['filename_b64'], c.b64(b'debs/two.deb'))

    def test_plan_has_no_delete_side_effects(self):
        plan = c.cleanup_plan(self.records, self.ledger, self.snapshot, COMMIT, TREE)
        self.assertFalse(plan['enabled'])
        self.assertTrue(plan['advisory_only'])
        self.assertEqual(plan['count'], 2)
        self.assertEqual(plan['bytes'], 6)

    def test_load_catalog_and_delete_single_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'packages').mkdir()
            (root / 'catalog-state').mkdir()
            (root / 'catalog-state' / 'processed.json').write_bytes(c.encoded(self.ledger))
            for record in self.records:
                (root / 'packages' / (record['id'] + '.json')).write_bytes(c.encoded(record))
            self.assertEqual(len(c.load_catalog(root)[0]), 2)
            (root / 'packages' / (self.record['id'] + '.json')).unlink()
            self.assertEqual(len(c.load_catalog(root)[0]), 1)
            self.assertEqual(c.pending(self.snapshot, c.load_catalog(root)[1]), {})

    def test_git_tree_uses_bytes_ignores_readme(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / 'debs').mkdir()
            (root / 'debs' / '中文 包.deb').write_bytes(b'abc')
            (root / 'debs' / 'README.md').write_text('not an inbox item')
            subprocess.run(['git', '-C', str(root), 'add', 'debs'], check=True)
            subprocess.run(['git', '-C', str(root), '-c', 'user.name=test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture'], check=True)
            snapshot = c.tree_snapshot(root)
            self.assertEqual(set(snapshot), {'debs/中文 包.deb'.encode()})


class CleanupGates(unittest.TestCase):
    def setUp(self):
        self.records, self.ledger, self.snapshot = fixtures()
        self.plan = c.cleanup_plan(self.records, self.ledger, self.snapshot, COMMIT, TREE)
        self.receipt = {'commit': COMMIT, 'success': True,
                        'catalog_sha256': c.sha(c.encoded(sorted(self.records, key=lambda r: r['id']))),
                        'index_sha256': c.sha(c.render(sorted(self.records, key=lambda r: r['id']))),
                        'verified_objects': {r['object_key']: {'sha256': r['sha256'], 'size': r['size'],
                            'head_metadata_verified': True, 'full_get_hash_verified': True} for r in self.records}}

    def candidates(self, **kwargs):
        args = dict(plan=self.plan, records=self.records, ledger=self.ledger, receipt=self.receipt,
                    current_commit=COMMIT, snapshot=self.snapshot, enabled=True,
                    approved_plan_sha256=c.sha(c.encoded(self.plan)))
        args.update(kwargs)
        return g.cleanup_candidates(**args)

    def test_default_disabled_even_with_invalid_receipt(self):
        self.assertEqual(g.cleanup_candidates(None, None, None, None, None, None), [])

    def test_false_string_never_enables(self):
        self.assertEqual(self.candidates(enabled='false'), [])

    def test_only_exact_approved_plan_passes(self):
        self.assertEqual(len(self.candidates()), 2)

    def test_unapproved_plan_refused(self):
        with self.assertRaises(ValueError): self.candidates(approved_plan_sha256=None)

    def test_failed_publication_refused(self):
        self.receipt['success'] = False
        with self.assertRaises(ValueError): self.candidates()

    def test_failed_upload_missing_hash_refused(self):
        self.receipt['verified_objects'] = {}
        with self.assertRaises(ValueError): self.candidates()

    def test_head_without_full_get_refused(self):
        next(iter(self.receipt['verified_objects'].values()))['full_get_hash_verified'] = False
        with self.assertRaises(ValueError): self.candidates()

    def test_new_commit_race_refused(self):
        with self.assertRaises(ValueError): self.candidates(current_commit=B)

    def test_same_path_blob_replacement_refused(self):
        snapshot = dict(self.snapshot)
        snapshot[b'debs/test.deb'] = B
        with self.assertRaises(ValueError): self.candidates(snapshot=snapshot)

    def test_catalog_deleted_after_publication_refused(self):
        with self.assertRaises(ValueError): self.candidates(records=self.records[1:])

    def test_different_receipt_commit_refused(self):
        self.receipt['commit'] = B
        with self.assertRaises(ValueError): self.candidates()

    def test_changed_index_hash_refused(self):
        self.receipt['index_sha256'] = 'f' * 64
        with self.assertRaises(ValueError): self.candidates()


if __name__ == '__main__':
    unittest.main(verbosity=2)
