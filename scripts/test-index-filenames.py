#!/usr/bin/env python3
"""Exercise build.sh's actual validator; no deb installation or copied parser."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BUILD = (ROOT / 'build.sh').read_text(encoding='utf-8')
VALIDATOR = BUILD.split("python3 <<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
PATH_VALIDATOR = (ROOT / 'validate-repo.sh').read_text(encoding='utf-8').split(
    "python3 <<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
NORMAL = 'debs/com.snapbackup_3.6.5_iphoneos-arm64e.deb'


class FilenameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'debs').mkdir()

    def entries(self, names):
        entries = []
        for i, name in enumerate(names):
            payload = ('validator fixture %d' % i).encode()
            (self.root / name).write_bytes(payload)
            entries.append({
                'Package': 'test.filename%d' % i, 'Version': '1.0',
                'Architecture': 'iphoneos-arm64e' if name == NORMAL else 'iphoneos-arm64',
                'Filename': name, 'Size': str(len(payload)),
                'SHA256': hashlib.sha256(payload).hexdigest(),
                'Description': '保留字段: 空格 & Unicode \u0085 \u2028 \u2029',
            })
        return entries

    def write_index(self, entries, eol='\n', final_newline=True):
        text = '\n\n'.join('\n'.join(k + ': ' + v for k, v in e.items()) for e in entries)
        if final_newline:
            text += '\n\n'
        (self.root / 'Packages').write_bytes(text.replace('\n', eol).encode('utf-8', 'surrogateescape'))

    def run_validator(self, source=VALIDATOR, expected=None):
        if expected is not None:
            # Check all parsed fields, not merely the validator's exit status.
            source += '\nassert entries == ' + repr(expected) + ', repr(entries)\n'
        return subprocess.run([sys.executable, '-c', source], cwd=self.root,
                              capture_output=True, text=True)

    def assert_valid(self, entries):
        result = self.run_validator(expected=entries)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unicode_separators_spaces_chinese_lf_and_crlf(self):
        names = [NORMAL, 'debs/中文 备份 & a\u030a.deb']
        names += ['debs/before' + c + 'after.deb' for c in
                  ['\u0085', '\u2028', '\u2029', '\v', '\f', '\x1c', '\x1d', '\x1e']]
        entries = self.entries(names)
        for eol in ['\n', '\r\n']:
            for final_newline in [True, False]:
                with self.subTest(eol=repr(eol), final_newline=final_newline):
                    self.write_index(entries, eol, final_newline)
                    self.assert_valid(entries)

    def test_real_snapbackup_filename_and_old_parser_failure(self):
        paths = subprocess.check_output(
            ['git', 'ls-tree', '-r', '-z', '--name-only', 'HEAD', '--', 'debs/'], cwd=ROOT
        ).decode('utf-8').split('\0')
        names = [p for p in paths if p.startswith('debs/SnapBackup ') and '\u0085' in p]
        self.assertEqual(len(names), 1, 'Expected the unchanged SnapBackup regression filename')
        entries = self.entries(names + [NORMAL])
        self.write_index(entries)
        self.assert_valid(entries)
        self.assertIn('for line in text.split("\\n"):', VALIDATOR)
        original = VALIDATOR.replace('for line in text.split("\\n"):',
                                     'for line in text.splitlines():').replace(
                                         'read_text(encoding="utf-8")',
                                         'read_text(encoding="utf-8", errors="replace")')
        result = self.run_validator(original)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Packages 指向不存在的文件', result.stderr)

    def test_missing_file_still_rejected(self):
        entries = self.entries(['debs/中文 \u0085 missing.deb', NORMAL])
        self.write_index(entries)
        (self.root / entries[0]['Filename']).unlink()
        result = self.run_validator()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(entries[0]['Filename'], result.stderr)
        self.assertIn('Packages 指向不存在的文件', result.stderr)

    def test_legacy_control_bytes_are_preserved_without_rewriting_index(self):
        entries = self.entries([NORMAL])
        self.write_index(entries)
        index = self.root / 'Packages'
        raw = index.read_bytes().replace(b'Description: ', b'Description: legacy \xe5\xbe ')
        index.write_bytes(raw)
        result = self.run_validator(source=VALIDATOR + '\nassert text.encode("utf-8", "surrogateescape") == packages.read_bytes()\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(index.read_bytes(), raw)

    def test_non_utf8_filename_does_not_match_replacement_character_path(self):
        entries = self.entries(['debs/legacy-\ufffd.deb'])
        self.write_index(entries)
        index = self.root / 'Packages'
        index.write_bytes(index.read_bytes().replace(b'\xef\xbf\xbd', b'\xff'))
        result = self.run_validator()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Packages 指向不存在的文件', result.stderr)

    def test_required_fields_architecture_and_unsafe_paths(self):
        for field, value in [('SHA256', None), ('Architecture', 'unsupported'),
                             ('Filename', '/tmp/absolute.deb'), ('Filename', 'debs/../escape.deb')]:
            with self.subTest(field=field, value=value):
                entries = self.entries([NORMAL])
                if value is None:
                    del entries[0][field]
                else:
                    entries[0][field] = value
                self.write_index(entries)
                self.assertNotEqual(self.run_validator().returncode, 0)

    def test_empty_index_still_rejected(self):
        (self.root / 'Packages').write_bytes(b'')
        self.assertNotEqual(self.run_validator().returncode, 0)

    def test_independent_path_check_preserves_bytes_and_crlf(self):
        entries = self.entries(['debs/中文 \u0085 \u2028 \u2029 & backup.deb', NORMAL,
                                'debs/trailing-space.deb '])
        for eol in ['\n', '\r\n']:
            self.write_index(entries, eol)
            result = self.run_validator(PATH_VALIDATOR)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assert_valid(entries)
        (self.root / entries[0]['Filename']).unlink()
        result = self.run_validator(PATH_VALIDATOR)
        self.assertNotEqual(result.returncode, 0)

    def test_size_and_hash_mismatch_still_rejected(self):
        for field, value in [('Size', '0'), ('SHA256', '0' * 64)]:
            entries = self.entries([NORMAL])
            entries[0][field] = value
            self.write_index(entries)
            self.assertNotEqual(self.run_validator().returncode, 0)

    def test_strict_utf8_regression_mutant_fails(self):
        entries = self.entries([NORMAL])
        entries[0]['Description'] = 'legacy \udcff'
        self.write_index(entries)
        mutant = VALIDATOR.replace('errors="surrogateescape"', 'errors="strict"')
        self.assertNotEqual(self.run_validator(mutant).returncode, 0)
        self.assert_valid(entries)

    def test_depiction_filter_preserves_filename_bytes(self):
        entries = self.entries(['debs/中文 \u0085 \u2028 \u2029 & backup.deb', NORMAL])
        for package in ['test.unrelated', 'org.shallowdream.dockpages']:
            with self.subTest(package=package):
                entries[0]['Package'] = package
                self.write_index(entries)
                result = subprocess.run(['bash', str(ROOT / 'scripts/apply-depictions.sh')],
                                        input=(self.root / 'Packages').read_bytes(), capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                for entry in entries:
                    self.assertIn(('Filename: ' + entry['Filename'] + '\n').encode(), result.stdout)


if __name__ == '__main__':
    unittest.main(verbosity=2)
