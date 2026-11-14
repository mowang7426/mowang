#!/usr/bin/env python3
"""Real local Git/archive/build tests; network isolation E2E is separate."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import catalog as c

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('importer', ROOT/'scripts/import-catalog.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def run(root, *args, data=None, env=None):
    return subprocess.check_output(['git', '-C', str(root), *args], input=data, env=env, stderr=subprocess.DEVNULL).decode().strip()


class LocalAPI:
    def __init__(self, remote):
        self.remote = remote
        self.race = None
        self.commits = 0

    def head(self, branch):
        return run(self.remote, 'rev-parse', 'refs/heads/'+branch)

    def edit(self, branch, parent, changes):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, GIT_INDEX_FILE=directory+'/index')
            run(self.remote, 'read-tree', parent, env=env)
            for path, data in changes.items():
                if data is None:
                    run(self.remote, 'update-index', '--index-info', data=('0 ' + '0'*40 + '\t' + path + '\n').encode(), env=env)
                else:
                    blob = run(self.remote, 'hash-object', '-w', '--stdin', data=data)
                    run(self.remote, 'update-index', '--add', '--cacheinfo', '100644', blob, path, env=env)
            tree = run(self.remote, 'write-tree', env=env)
            return run(self.remote, 'commit-tree', tree, '-p', parent, '-m', 'fixture')

    def commit(self, branch, parent, changes):
        self.commits += 1
        child = self.edit(branch, parent, changes)
        if self.race:
            race, self.race = self.race, None
            race(parent)
        result = subprocess.run(['git', '-C', str(self.remote), 'update-ref', 'refs/heads/'+branch, child, parent], capture_output=True)
        return child if result.returncode == 0 else None


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base/'local'
        self.root.mkdir()
        run(self.root, 'init', '-b', 'main')
        run(self.root, 'config', 'user.email', 'catalog-test@example.invalid')
        run(self.root, 'config', 'user.name', 'Catalog isolation test')
        (self.root/'debs').mkdir()
        (self.root/'packages').mkdir()
        (self.root/'catalog-state').mkdir()
        self.records = []
        for name in ('one', 'two'):
            package = self.package(name)
            destination = self.root/'debs'/f'{name}.deb'
            destination.write_bytes(package)
        run(self.root, 'add', 'debs')
        run(self.root, 'commit', '-m', 'inert fixture sources')
        for path, blob in c.tree_snapshot(self.root).items():
            with tempfile.TemporaryDirectory() as directory:
                record, _ = m.archived_record(self.root, path, blob, Path(directory))
            self.records.append(record)
            (self.root/'packages'/(record['id']+'.json')).write_bytes(c.encoded(record))
        self.ledger = {'schema': 1, 'processed': {c.source_token(r['source']): c.ledger_row(r) for r in self.records}}
        (self.root/'catalog-state/processed.json').write_bytes(c.encoded(self.ledger))
        (self.root/'unrelated.txt').write_text('user data\n')
        run(self.root, 'add', '.')
        run(self.root, 'commit', '-m', 'catalog fixture')
        self.remote = self.base/'remote.git'
        subprocess.run(['git', 'clone', '--bare', str(self.root), str(self.remote)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        run(self.remote, 'config', 'user.email', 'catalog-test@example.invalid')
        run(self.remote, 'config', 'user.name', 'Catalog isolation test')
        run(self.root, 'remote', 'add', 'origin', str(self.remote))
        self.api = LocalAPI(self.remote)
        self.uploaded = []

    def tearDown(self):
        self.tmp.cleanup()

    def package(self, name):
        directory = self.base/('pkg-'+name)
        (directory/'DEBIAN').mkdir(parents=True, exist_ok=True)
        (directory/'DEBIAN/control').write_text(f'Package: test.catalog.{name}\nVersion: 1.0\nArchitecture: iphoneos-arm64\nMaintainer: Test <test@example.invalid>\nDescription: inert no-script fixture\nSection: Tweaks\n')
        (directory/'data.txt').write_text(name)
        output = self.base/(name+'.deb')
        subprocess.run(['dpkg-deb', '-Zgzip', '--build', str(directory), str(output)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return output.read_bytes()

    def append(self, data=None):
        raw = self.package('three') if data is None else data
        parent = self.api.head('main')
        commit = self.api.edit('main', parent, {'debs/three.deb': raw})
        run(self.remote, 'update-ref', 'refs/heads/main', commit, parent)
        return commit

    def upload(self, record, path):
        self.assertEqual(c.sha(path.read_bytes()), record['sha256'])
        self.uploaded.append(record)
        return 'managed'

    def txn(self):
        return m.transaction(self.root, 'main', self.api, self.upload)

    def inspect(self, commit):
        subprocess.run(['git', '-C', str(self.root), 'fetch', '-q', 'origin', commit], check=True)
        return m.committed_catalog(self.root, commit)

    def test_noop_does_not_commit_or_upload(self):
        result = self.txn()
        self.assertEqual(result['imported'], 0)
        self.assertEqual(self.api.commits, 0)
        self.assertFalse(self.uploaded)

    def test_verified_upload_commits_catalog_ledger_preserves_inbox_and_other_files(self):
        parent = self.append()
        result = self.txn()
        records, ledger = self.inspect(result['commit'])
        self.assertEqual((len(records), len(ledger['processed']), result['imported']), (3, 3, 1))
        self.assertEqual(run(self.remote, 'show', result['commit']+':unrelated.txt'), 'user data')
        self.assertEqual(run(self.remote, 'rev-parse', parent+':debs'), run(self.remote, 'rev-parse', result['commit']+':debs'))
        self.assertEqual(run(self.root, 'rev-parse', 'HEAD'), run(self.root, 'rev-parse', 'main'))

    def test_bad_new_archive_never_uploads_or_commits(self):
        before = self.append(b'not a deb archive')
        with self.assertRaises(subprocess.CalledProcessError):
            self.txn()
        self.assertEqual(self.api.head('main'), before)
        self.assertFalse(self.uploaded)

    def test_upload_failure_never_commits(self):
        before = self.append()
        def reject(*args):
            raise ValueError('failed S3/public evidence')
        with self.assertRaises(ValueError):
            m.transaction(self.root, 'main', self.api, reject)
        self.assertEqual(self.api.head('main'), before)
        self.assertEqual(self.api.commits, 0)

    def test_deleted_catalog_stale_inbox_never_resurrects(self):
        parent = self.api.head('main')
        name = 'packages/'+self.records[0]['id']+'.json'
        changed = self.api.edit('main', parent, {name: None})
        run(self.remote, 'update-ref', 'refs/heads/main', changed, parent)
        result = self.txn()
        records, ledger = self.inspect(result['commit'])
        self.assertEqual((len(records), len(ledger['processed'])), (1, 2))
        self.assertFalse(self.uploaded)
        self.assertEqual(result['imported'], 0)

    def test_cas_retry_preserves_concurrent_catalog_and_inbox_deletion(self):
        self.append()
        name = 'packages/'+self.records[0]['id']+'.json'
        def race(parent):
            changed = self.api.edit('main', parent, {name: None, 'debs/three.deb': None, 'unrelated.txt': b'new user bytes\n'})
            run(self.remote, 'update-ref', 'refs/heads/main', changed, parent)
        self.api.race = race
        result = self.txn()
        records, ledger = self.inspect(result['commit'])
        self.assertEqual((len(records), len(ledger['processed']), result['attempts']), (1, 2, 2))
        self.assertEqual(run(self.remote, 'show', result['commit']+':unrelated.txt'), 'new user bytes')
        self.assertEqual(result['imported'], 0)

    def test_same_filename_new_blob_after_cas_race_imports_only_new_blob(self):
        self.append()
        replacement = self.package('replacement')
        def race(parent):
            changed = self.api.edit('main', parent, {'debs/three.deb': replacement})
            run(self.remote, 'update-ref', 'refs/heads/main', changed, parent)
        self.api.race = race
        result = self.txn()
        records, ledger = self.inspect(result['commit'])
        self.assertEqual((len(records), len(ledger['processed']), result['attempts']), (3, 3, 2))
        self.assertEqual(len(self.uploaded), 2)
        self.assertEqual({r['sha256'] for r in records} - {r['sha256'] for r in self.records}, {c.sha(replacement)})

    def test_noop_rechecks_head_before_success(self):
        oldhead = self.api.head
        calls = []
        def head(branch):
            if len(calls) == 1:
                self.append()
            calls.append(True)
            return oldhead(branch)
        # Avoid recursive append -> api.head in the injected hook.
        parent = oldhead('main')
        changed = self.api.edit('main', parent, {'unrelated.txt': b'advanced\n'})
        def advancing(branch):
            calls.append(True)
            if len(calls) == 2:
                run(self.remote, 'update-ref', 'refs/heads/main', changed, parent)
            return oldhead(branch)
        self.api.head = advancing
        result = self.txn()
        self.assertEqual(result['commit'], changed)
        self.assertEqual(result['attempts'], 2)

    def test_cas_retry_cap_does_not_force(self):
        self.append()
        self.api.commit = lambda *args: None
        with self.assertRaises(RuntimeError):
            m.transaction(self.root, 'main', self.api, self.upload, attempts=2)
        self.assertEqual(len(self.uploaded), 1)

    def copy_build(self):
        for name in ('build.sh', 'check-debs.sh', 'validate-repo.sh'):
            shutil.copy2(ROOT/name, self.root/name)
        shutil.copytree(ROOT/'scripts', self.root/'scripts', ignore=shutil.ignore_patterns('__pycache__'))

    def test_build_empty_inbox_keeps_catalog_root_and_site_identical(self):
        self.copy_build()
        shutil.rmtree(self.root/'debs')
        subprocess.run(['bash', 'build.sh'], cwd=self.root, check=True, stdout=subprocess.DEVNULL)
        subprocess.run(['bash', 'validate-repo.sh'], cwd=self.root, check=True, stdout=subprocess.DEVNULL)
        env = dict(os.environ, R2_PUBLIC_BASE_URL='https://example.invalid/repo')
        subprocess.run(['bash', 'scripts/update-repository.sh'], cwd=self.root, env=env, check=True, stdout=subprocess.DEVNULL)
        self.assertEqual((self.root/'Packages').read_bytes(), c.render(self.records))
        self.assertEqual((self.root/'_site/Packages').read_bytes(), c.render(self.records))

    def test_local_bad_new_upload_rejected(self):
        self.copy_build()
        (self.root/'debs/bad.deb').write_bytes(b'invalid new upload')
        result = subprocess.run(['python3', 'scripts/catalog-build.py', 'check'], cwd=self.root, capture_output=True)
        self.assertNotEqual(result.returncode, 0)

    def test_unlisted_record_not_reintroduced_by_build(self):
        self.copy_build()
        (self.root/'packages'/(self.records[0]['id']+'.json')).unlink()
        subprocess.run(['python3', 'scripts/catalog-build.py', 'render'], cwd=self.root, check=True, stdout=subprocess.DEVNULL)
        self.assertEqual((self.root/'Packages').read_bytes(), c.render([self.records[1]]))

    def test_missing_catalog_directory_never_falls_back_to_inbox(self):
        self.copy_build()
        shutil.rmtree(self.root/'packages')
        result = subprocess.run(['python3', 'scripts/catalog-build.py', 'render'], cwd=self.root, capture_output=True)
        self.assertNotEqual(result.returncode, 0)


class ProtocolTests(unittest.TestCase):
    def test_github_commit_is_single_parent_nonforce_and_base_tree_patch(self):
        class API(m.GitHub):
            def __init__(self):
                self.calls = []
            def request(self, method, path, value=None):
                self.calls.append((method, path, value))
                if method == 'GET':
                    return {'tree': {'sha': 'user-current-tree'}}
                if path == '/git/blobs':
                    return {'sha': 'new-blob'}
                if path == '/git/trees':
                    return {'sha': 'patched-tree'}
                if path == '/git/commits':
                    return {'sha': 'single-parent-commit'}
                return {}
        api = API()
        self.assertEqual(api.commit('branch/test', 'observed-head', {'packages/new.json': b'{}'}), 'single-parent-commit')
        tree = next(value for method, path, value in api.calls if path == '/git/trees')
        commit = next(value for method, path, value in api.calls if method == 'POST' and path == '/git/commits')
        self.assertEqual(tree['base_tree'], 'user-current-tree')
        self.assertEqual(len(tree['tree']), 1)
        self.assertEqual(commit['parents'], ['observed-head'])
        self.assertEqual(api.calls[-1], ('PATCH', '/git/refs/heads/branch/test', {'sha': 'single-parent-commit', 'force': False}))

    def test_generated_checksums_do_not_reencode_nonutf8_control_bytes(self):
        block = b'Package: test\nDescription: legacy \xff\x85\n continued\nMD5sum: bad\nSHA1: bad\nSHA256: bad'
        result = m.archive_metadata(block, b'exact archive bytes')
        self.assertIn(b'Description: legacy \xff\x85\n continued\n', result)
        self.assertIn(b'SHA256: '+c.sha(b'exact archive bytes').encode(), result)
        self.assertNotIn(b': bad', result)


if __name__ == '__main__':
    unittest.main()
