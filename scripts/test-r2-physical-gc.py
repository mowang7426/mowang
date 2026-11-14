#!/usr/bin/env python3
import copy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location('gc', Path(__file__).with_name('r2-physical-gc.py'))
gc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gc)
K = 'debs/' + 'a' * 64 + '.deb'
ROW = {'sha256': 'a' * 64, 'size': 3, 'refs': 2, 'ownership': 'managed'}


def snapshot(refs=2, external=False):
    row = dict(ROW, refs=refs, ownership='external' if external else 'managed')
    return {'objects': {K: row} if refs else {}, 'garbage': {K: 0}}


def state():
    return gc.observe(None, snapshot(), '1' * 64, 0)


def mature():
    first = gc.observe(state(), snapshot(0), '2' * 64, 1)
    return gc.observe(first, snapshot(0), '3' * 64, 1 + gc.GRACE)


class FutureOnly(unittest.TestCase):
    def test_empty_baseline_does_not_adopt_garbage(self):
        self.assertEqual(gc.observe(None, snapshot(0), '1' * 64, 0)['pending'], {})

    def test_shared_reference_retains_object(self):
        self.assertEqual(gc.observe(state(), snapshot(1), '2' * 64, 1)['pending'], {})

    def test_last_reference_requires_delay(self):
        first = gc.observe(state(), snapshot(0), '2' * 64, 1)
        self.assertEqual(gc.candidates(first, 2 * gc.GRACE), {})
        second = gc.observe(first, snapshot(0), '3' * 64, 2)
        self.assertEqual(gc.candidates(second, gc.GRACE), {})
        self.assertEqual(list(gc.candidates(second, gc.GRACE + 1)), [K])

    def test_same_receipt_not_confirmation(self):
        first = gc.observe(state(), snapshot(0), '2' * 64, 1)
        self.assertEqual(gc.observe(first, snapshot(0), '2' * 64, gc.GRACE)['pending'][K]['confirmations'], 1)

    def test_receipt_content_reuse_rejected(self):
        with self.assertRaises(ValueError):
            gc.observe(state(), snapshot(0), '1' * 64, 1)

    def test_external_never_candidate(self):
        first = gc.observe(None, snapshot(external=True), '1' * 64, 0)
        self.assertEqual(gc.observe(first, snapshot(0), '2' * 64, 1)['pending'], {})

    def test_restore_cancels_event(self):
        restored = gc.observe(mature(), snapshot(1), '4' * 64, 2 * gc.GRACE)
        self.assertEqual(gc.candidates(restored, 3 * gc.GRACE), {})

    def test_remove_after_restore_starts_fresh_delay(self):
        restored = gc.observe(mature(), snapshot(1), '4' * 64, 2 * gc.GRACE)
        deleted = gc.observe(restored, snapshot(0), '5' * 64, 2 * gc.GRACE + 1)
        self.assertEqual(deleted['pending'][K]['removed_at'], 2 * gc.GRACE + 1)

    def test_baseline_does_not_mutate_source(self):
        original = snapshot()
        s = gc.observe(None, original, '1' * 64, 0)
        s['objects'][K]['size'] = 7
        self.assertEqual(original['objects'][K]['size'], 3)

    def test_backward_clock_rejected(self):
        with self.assertRaises(ValueError):
            gc.observe(mature(), snapshot(0), '4' * 64, 0)

    def test_bad_key_rejected(self):
        with self.assertRaises(ValueError):
            gc.observe(None, {'objects': {'debs/../Packages': ROW}}, '1' * 64, 0)

    def test_production_sealed_even_empty_plan(self):
        client = Mock()
        with self.assertRaises(ValueError):
            gc.delete_isolated(client, 'b', 'repo/', gc.OWNER, mature(), gc.GRACE + 1, lambda: '3' * 64)
        client.delete_object.assert_not_called()

    def test_foreign_owner_protected(self):
        client = Mock()
        client.head_object.return_value = {'ContentLength': 3, 'ETag': 'old', 'Metadata': {}}
        result = gc.delete_isolated(client, 'b', 'catalog-e2e/r2-gc-unit/', gc.TEST_OWNER,
                                    mature(), gc.GRACE + 1, lambda: '3' * 64)
        self.assertEqual(len(result['protected']), 1)
        client.delete_object.assert_not_called()

    def test_new_head_refuses_before_s3(self):
        client = Mock()
        with self.assertRaises(ValueError):
            gc.delete_isolated(client, 'b', 'catalog-e2e/r2-gc-unit/', gc.TEST_OWNER,
                               mature(), gc.GRACE + 1, lambda: '4' * 64)
        client.head_object.assert_not_called()

    def test_race_after_head_refuses_delete(self):
        client = Mock()
        client.head_object.return_value = {'ContentLength': 3, 'ETag': 'old', 'Metadata': {
            'owner': gc.TEST_OWNER, 'owner-version': '1', 'sha256': 'a' * 64, 'size': '3'}}
        gate = Mock(side_effect=['3' * 64, '4' * 64])
        with self.assertRaises(ValueError):
            gc.delete_isolated(client, 'b', 'catalog-e2e/r2-gc-unit/', gc.TEST_OWNER,
                               mature(), gc.GRACE + 1, gate)
        client.delete_object.assert_not_called()


if __name__ == '__main__':
    unittest.main()
