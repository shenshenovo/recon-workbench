import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from scripts.quality_duplicate_inputs import freeze, prepare, verify_frozen
from scripts.quality_duplicate_review import REVIEW_FIELDS, reviewed_exclusions
from scripts.quality_duplicate_results import aggregate, run_order


def write_review(path, rows):
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=REVIEW_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def review_row(pair_id, a, b, decision='exclude_one', exclude=None, retain=None):
    return dict(candidate_id=pair_id, a=a, b=b, decision=decision,
                exclude=exclude or b, retain=retain or a,
                reviewer='assistant_visual_review', notes='Same photograph version.')


class DuplicateReviewTests(unittest.TestCase):
    def test_unflagged_copy_and_quality_warning_do_not_get_excluded(self):
        candidates = [dict(candidate_id='D001', a='a.jpg', b='copy.jpg')]
        names = ['a.jpg', 'blurry.jpg', 'copy.jpg', 'missed.jpg']
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'review.csv'
            write_review(path, [review_row('D001', 'a.jpg', 'copy.jpg')])
            excluded = reviewed_exclusions(path, candidates, names, ['a.jpg', 'blurry.jpg'])
        self.assertEqual([row['name'] for row in excluded], ['copy.jpg'])
        self.assertEqual(set(names) - {row['name'] for row in excluded},
                         {'a.jpg', 'blurry.jpg', 'missed.jpg'})

    def test_rejects_chained_exclusions_without_a_retained_reference(self):
        candidates = [dict(candidate_id='D001', a='a', b='b'),
                      dict(candidate_id='D002', a='b', b='c')]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'review.csv'
            write_review(path, [review_row('D001', 'a', 'b'), review_row('D002', 'b', 'c')])
            with self.assertRaisesRegex(ValueError, 'reference.*excluded'):
                reviewed_exclusions(path, candidates, ['a', 'b', 'c'], ['a'])

    def test_rejects_fabricated_candidate_or_changed_pair_identity(self):
        candidates = [dict(candidate_id='D001', a='a', b='b')]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'review.csv'
            write_review(path, [review_row('D001', 'a', 'c')])
            with self.assertRaisesRegex(ValueError, 'identity'):
                reviewed_exclusions(path, candidates, ['a', 'b', 'c'], ['a'])

    def test_fixed_originals_cannot_be_removed_and_all_rows_need_review(self):
        candidates = [dict(candidate_id='D001', a='a', b='b')]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'review.csv'
            write_review(path, [review_row('D001', 'a', 'b', exclude='a', retain='b')])
            with self.assertRaisesRegex(ValueError, 'original'):
                reviewed_exclusions(path, candidates, ['a', 'b'], ['a'])
            write_review(path, [review_row('D001', 'a', 'b', decision='')])
            with self.assertRaisesRegex(ValueError, 'decision'):
                reviewed_exclusions(path, candidates, ['a', 'b'], ['a'])


class DuplicateReportTests(unittest.TestCase):
    def test_rotation_has_three_independent_attempts_per_group(self):
        order = run_order()
        self.assertEqual(len(set(order)), 9)
        self.assertEqual([name for repeat, name in order if repeat == 2],
                         ['augmented', 'filtered', 'original'])

    def test_partial_failure_keeps_common_denominator_and_no_success_claim(self):
        manifest = {'original_denominator': 60, 'groups': {
            name: {'names': list(range(count)), 'excluded': []}
            for name, count in [('original', 60), ('augmented', 75), ('filtered', 63)]}}
        run = {'group': 'augmented', 'repeat': 1, 'returncode': 1,
               'elapsed_seconds': 10, 'summary': {}, 'original_coverage': 0,
               'registered_original_names': [], 'pose_reference_consistency': {}}
        report = aggregate([run], manifest)
        self.assertFalse(report['complete'])
        self.assertFalse(report['all_runs_successful'])
        self.assertEqual(report['expected_runs'], 9)
        self.assertEqual(report['original_denominator'], 60)
        self.assertEqual(report['groups']['augmented']['successful'], 0)
        self.assertIsNone(report['groups']['augmented']['metrics']['original_coverage'])
        self.assertEqual(report['groups']['augmented']['metrics']['elapsed_seconds']['median'], 10)


class DuplicateFrozenInputTests(unittest.TestCase):
    def test_prefix_variants_and_frozen_review_are_enforced(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source'
            images = source / 'images'
            images.mkdir(parents=True)
            rng = np.random.default_rng(73)
            frames = []
            for index in range(61):
                name = f'frame_{index:05}.jpg'
                Image.fromarray(rng.integers(30, 220, (48, 64, 3), dtype=np.uint8)).save(images / name)
                frames.append({'file_path': './images/' + name})
            (source / 'transforms.json').write_text(json.dumps({'frames': frames}))
            output = root / 'experiment'
            prepared = prepare(source, output)
            self.assertEqual(len(prepared['originals']), 60)
            self.assertEqual(prepared['originals'][-1]['name'], 'frame_00059.jpg')
            self.assertEqual(len({row['source_name'] for row in prepared['variants']}), 15)
            for kind in ('exact_copy', 'jpeg_reencode', 'resize'):
                self.assertEqual(sum(row['kind'] == kind for row in prepared['variants']), 5)
            candidates = json.loads((output / 'candidates.json').read_text())
            rows = [dict(candidate_id=row['candidate_id'], a=row['a'], b=row['b'],
                         decision='keep_both', exclude='', retain='', reviewer='test',
                         notes='Explicitly retain this pair.') for row in candidates]
            write_review(output / 'review.csv', rows)
            with patch('scripts.quality_duplicate_inputs.provenance',
                       return_value={'files_sha256': {}, 'environment': {}}):
                manifest = freeze(output)
            self.assertEqual(len(manifest['groups']['filtered']['names']), 75)
            verify_frozen(output, manifest)
            with (output / 'review.csv').open('a') as stream:
                stream.write('\n')
            with self.assertRaisesRegex(ValueError, 'review or evidence changed'):
                verify_frozen(output, manifest)


if __name__ == '__main__':
    unittest.main()
