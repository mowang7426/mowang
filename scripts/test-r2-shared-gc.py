#!/usr/bin/env python3
"""Integrated mutex + exact production publisher + delayed physical GC regressions."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from botocore.exceptions import ClientError
import r2_shared_lock as locks
import r2_gc_publication as integration


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(file))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fixtures = load('publication_fixtures', 'test-catalog-publication.py')
p = fixtures.p
base_s3 = fixtures.MemoryS3


class SharedMutex(unittest.TestCase):
    def setUp(self):
        self.s = base_s3()
        self.env = patch.dict(os.environ, {'GITHUB_RUN_ID': '101', 'GITHUB_RUN_ATTEMPT': '1'}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_double_job_conflict_no_takeover(self):
        a = locks.Mutex(self.s, 'b', 'repo/').acquire()
        with self.assertRaises((RuntimeError, ValueError)):
            locks.Mutex(self.s, 'b', 'repo/').acquire()
        a.verify()
        a.release()

    def test_same_run_still_has_unique_owner(self):
        a = locks.Mutex(self.s, 'b', 'repo/')
        b = locks.Mutex(self.s, 'b', 'repo/')
        self.assertNotEqual(a.body, b.body)

    def test_borrowed_lock_does_not_release_or_deadlock(self):
        a = locks.Mutex(self.s, 'b', 'repo/').acquire()
        with patch.dict(os.environ, {'R2_SHARED_LOCK_TOKEN': a.token}):
            with locks.hold(self.s, 'b', 'repo/') as b:
                b.verify()
        a.verify()
        self.assertEqual(self.s.deleted, [])
        a.release()

    def test_wrong_token_refuses(self):
        locks.Mutex(self.s, 'b', 'repo/').acquire()
        with patch.dict(os.environ, {'R2_SHARED_LOCK_TOKEN': 'b' * 64}):
            with self.assertRaises(RuntimeError):
                with locks.hold(self.s, 'b', 'repo/'):
                    self.fail('borrowed another owner')
        self.assertEqual(self.s.deleted, [])

    def test_wrong_run_refuses(self):
        a = locks.Mutex(self.s, 'b', 'repo/').acquire()
        with patch.dict(os.environ, {'R2_SHARED_LOCK_TOKEN': a.token, 'GITHUB_RUN_ID': '102'}):
            with self.assertRaises(RuntimeError):
                with locks.hold(self.s, 'b', 'repo/'):
                    self.fail('borrowed another run')

    def test_lost_lock_cannot_release_foreign(self):
        a = locks.Mutex(self.s, 'b', 'repo/').acquire()
        self.s.data[a.key] = b'foreign'
        with self.assertRaises(RuntimeError):
            a.release()
        self.assertEqual(self.s.deleted, [])

    def test_stale_schema_not_taken_over(self):
        self.s.data['repo/' + locks.KEY] = b'{"schema":1}'
        with self.assertRaises((RuntimeError, ValueError)):
            locks.Mutex(self.s, 'b', 'repo/').acquire()
        self.assertEqual(self.s.deleted, [])

    def test_scope_forgery_refused(self):
        with patch.dict(os.environ, {'R2_TEST_PREFIX': 'catalog-e2e/r2-gc-x/'}):
            with self.assertRaises(ValueError):
                locks.scope()

    def test_production_clock_not_overridable(self):
        with patch.dict(os.environ, {'R2_GC_TEST_NOW': '99999999999'}):
            with self.assertRaises(ValueError):
                integration.clock('repo/')

    def test_foreign_schema_state_refuses(self):
        with self.assertRaises(ValueError):
            integration.validate_state({'schema': 1}, integration.gc.OWNER, 'repo/', 9)


class IntegratedPublisher(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.Pipeline()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.n = 1000
        self.now = 0
        self.patch = patch.object(integration, 'clock', side_effect=lambda prefix: self.now)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        # Faithful S3 HEAD404 for actually deleted fixture keys.
        original = self.f.s.head_object
        def head(Bucket, Key):
            if Key not in self.f.s.data:
                raise ClientError({'ResponseMetadata': {'HTTPStatusCode': 404}}, 'HeadObject')
            return original(Bucket, Key)
        self.f.s.head_object = head

    def publish(self, failure=None):
        self.n += 1
        self.f.run_pipeline(failure, str(self.n))

    def remove(self):
        r = self.f.records[1]
        self.path = Path('packages', r['id'] + '.json')
        self.original = self.path.read_bytes()
        self.path.unlink()
        self.f.commit()
        return r['object_key']

    def state(self):
        return json.loads(self.f.s.data[integration.STATE])['engine']

    def test_baseline_does_not_adopt_manifest_garbage(self):
        self.publish()
        self.assertEqual(self.state()['pending'], {})
        self.assertEqual(len(self.state()['objects']), 2)

    def test_real_publisher_last_reference_and_48h_delete(self):
        self.publish()
        key = self.remove()
        self.now = 1
        self.publish()
        self.assertIn(key, self.f.s.data)
        self.now = integration.gc.GRACE
        self.publish()
        self.assertIn(key, self.f.s.data)
        self.now += 1
        self.publish()
        self.assertNotIn(key, self.f.s.data)
        self.assertIn(key, self.f.s.deleted)
        self.assertEqual(self.state()['pending'], {})

    def test_failed_publish_does_not_advance_state_or_delete(self):
        self.publish()
        before = self.f.s.data[integration.STATE]
        key = self.remove()
        self.now = 10 * integration.gc.GRACE
        with self.assertRaises(ValueError):
            self.publish('public-index')
        self.assertEqual(before, self.f.s.data[integration.STATE])
        self.assertIn(key, self.f.s.data)

    def test_restore_cancels_mature_event(self):
        self.publish()
        key = self.remove()
        self.now = 1
        self.publish()
        self.path.write_bytes(self.original)
        self.f.commit()
        self.now = 3 * integration.gc.GRACE
        self.publish()
        self.assertIn(key, self.f.s.data)
        self.assertEqual(self.state()['pending'], {})

    def test_restored_json_missing_object_rejects_publication(self):
        self.publish()
        key = self.remove()
        self.now = 1
        self.publish()
        self.now += integration.gc.GRACE
        self.publish()
        self.path.write_bytes(self.original)
        self.f.commit()
        before = self.f.s.data['Release']
        with self.assertRaises(ClientError):
            self.publish()
        self.assertEqual(before, self.f.s.data['Release'])

    def test_foreign_state_refuses(self):
        self.publish()
        self.f.s.meta[integration.STATE]['owner'] = 'external'
        with self.assertRaises(ValueError):
            self.publish()

    def test_foreign_candidate_never_deleted(self):
        self.publish()
        key = self.remove()
        self.now = 1
        self.publish()
        self.f.s.meta[key]['owner'] = 'external'
        self.now += integration.gc.GRACE
        self.publish()
        self.assertIn(key, self.f.s.data)
        self.assertNotIn(key, self.f.s.deleted)

    def test_stale_head_never_updates_state(self):
        self.publish()
        before = self.f.s.data[integration.STATE]
        self.remove()
        with self.assertRaises(RuntimeError):
            self.publish('race')
        self.assertEqual(before, self.f.s.data[integration.STATE])


if __name__ == '__main__':
    unittest.main()
