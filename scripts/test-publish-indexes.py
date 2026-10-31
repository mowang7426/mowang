#!/usr/bin/env python3
"""Offline bare-Git regression tests; never touch the real repository."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).with_name('publish-indexes.sh').resolve()
GIT = shutil.which('git')
FILES = ['Packages', 'Packages.gz', 'Packages.bz2', 'Packages.xz', 'Packages.zst', 'Release']


def git(cwd, *args):
    return subprocess.check_output([GIT, '-C', str(cwd), *args], stderr=subprocess.STDOUT, text=True).strip()


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.remote = self.root / 'remote.git'
        self.work = self.root / 'build'
        self.other = self.root / 'uploader'
        git(self.root, 'init', '--bare', '--initial-branch=main', str(self.remote))
        git(self.root, 'clone', str(self.remote), str(self.work))
        for key, value in [('user.name', 'Test'), ('user.email', 'test@example.invalid')]:
            git(self.work, 'config', key, value)
        for name in FILES:
            (self.work / name).write_text('original\n')
        git(self.work, 'add', '.')
        git(self.work, 'commit', '-m', 'base')
        git(self.work, 'push', 'origin', 'main')
        self.base = git(self.work, 'rev-parse', 'HEAD')
        git(self.root, 'clone', str(self.remote), str(self.other))
        git(self.other, 'config', 'user.name', 'Uploader')
        git(self.other, 'config', 'user.email', 'uploader@example.invalid')
        self.env = dict(os.environ, INDEX_BASE_SHA=self.base, INDEX_BUILD_REF='refs/heads/main', LC_ALL='C')
        # Do not inherit any caller's repository-scoped Git variables.
        for key in ['GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE']:
            self.env.pop(key, None)

    def generate(self):
        for name in FILES:
            (self.work / name).write_text('generated\n')

    def advance(self):
        (self.other / 'uploaded.deb').write_text('new package snapshot\n')
        git(self.other, 'add', '.')
        git(self.other, 'commit', '-m', 'upload')
        git(self.other, 'push', 'origin', 'main')
        return git(self.other, 'rev-parse', 'HEAD')

    def run_script(self, success=True):
        p = subprocess.run(['bash', str(SCRIPT)], cwd=self.work, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(p.returncode == 0, success, p.stdout)
        return p.stdout

    def tip(self):
        return git(self.remote, 'rev-parse', 'main')

    def inject_push(self, kind):
        # Intercept exactly the push, after the helper has checked remote main.
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        wrapper = bin_dir / 'git'
        wrapper.write_text('#!/usr/bin/env python3\n' +
            'import os, subprocess, sys\n' +
            'real = ' + repr(GIT) + '\n' +
            'other = ' + repr(str(self.other)) + '\n' +
            'kind = ' + repr(kind) + '\n' +
            "if sys.argv[1:2] == ['push']:\n" +
            "    if kind in ('race', 'network_after_advance', 'permission_after_advance'):\n" +
            "        for args in [['add', '.'], ['commit', '-m', 'racing upload'], ['push', 'origin', 'main']]:\n" +
            "            subprocess.run([real, '-C', other] + args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n" +
            "    if kind != 'race':\n" +
            "        print('fatal: authentication denied' if kind == 'permission_after_advance' else 'fatal: network connection failed', file=sys.stderr)\n" +
            "        sys.exit(128)\n" +
            "os.execv(real, [real] + sys.argv[1:])\n")
        wrapper.chmod(0o755)
        (self.other / 'uploaded.deb').write_text('racing package snapshot\n')
        self.env['PATH'] = str(bin_dir) + os.pathsep + self.env['PATH']

    def test_normal_push(self):
        self.generate()
        self.assertIn('PUBLISHED:', self.run_script())
        self.assertEqual(git(self.remote, 'show', 'main:Packages'), 'generated')
        self.assertEqual(git(self.remote, 'rev-parse', 'main^'), self.base)

    def test_remote_advanced_before_check(self):
        self.generate()
        tip = self.advance()
        self.assertIn('SKIP: stale indexes', self.run_script())
        self.assertEqual(self.tip(), tip)
        self.assertEqual(git(self.work, 'rev-parse', 'HEAD'), self.base)

    def test_race_between_check_and_push(self):
        self.generate()
        self.inject_push('race')
        self.assertIn('SKIP: main advanced during publication', self.run_script())
        self.assertEqual(git(self.remote, 'show', 'main:Packages'), 'original')
        self.assertEqual(git(self.remote, 'show', 'main:uploaded.deb'), 'racing package snapshot')

    def test_server_hook_rejection_fails(self):
        self.generate()
        hook = self.remote / 'hooks' / 'pre-receive'
        hook.write_text('#!/bin/sh\necho denied >&2\nexit 1\n')
        hook.chmod(0o755)
        self.assertIn('ERROR: publication failed', self.run_script(False))
        self.assertEqual(self.tip(), self.base)

    def test_network_failure_even_when_main_advances(self):
        self.generate()
        self.inject_push('network_after_advance')
        self.assertIn('ERROR: publication failed', self.run_script(False))
        self.assertNotEqual(self.tip(), self.base)

    def test_permission_failure_even_when_main_advances(self):
        self.generate()
        self.inject_push('permission_after_advance')
        self.assertIn('authentication denied', self.run_script(False))
        self.assertNotEqual(self.tip(), self.base)

    def test_remote_unavailable_fails(self):
        self.generate()
        git(self.work, 'remote', 'set-url', 'origin', str(self.root / 'missing.git'))
        self.run_script(False)
        self.assertEqual(self.tip(), self.base)

    def test_non_main_validation_only(self):
        self.generate()
        self.env['INDEX_BUILD_REF'] = 'refs/heads/feature'
        self.assertIn('validation-only', self.run_script())
        self.assertEqual(self.tip(), self.base)

    def test_no_changes(self):
        self.assertIn('No generated changes', self.run_script())
        self.assertEqual(self.tip(), self.base)

    def test_wrong_base_fails(self):
        self.env['INDEX_BASE_SHA'] = '0' * 40
        self.assertIn('HEAD differs', self.run_script(False))

    def test_unexpected_staged_changes_fail(self):
        (self.work / 'unexpected').write_text('do not publish')
        git(self.work, 'add', 'unexpected')
        self.assertIn('unexpected staged changes', self.run_script(False))
        self.assertEqual(self.tip(), self.base)


if __name__ == '__main__':
    unittest.main(verbosity=2)
