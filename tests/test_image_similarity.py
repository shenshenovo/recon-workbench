import io
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from recon_workbench import image_quality as quality
from recon_workbench.image_similarity import similarity_report


def features(name, d=0, p=0, **values):
    return dict({'name': name, 'decodable': True, 'sha256': name, 'duplicate_of': None,
                 'similar_to': None, 'flags': [], 'width': 320, 'height': 240,
                 'luminance_std': 30, 'mean_luminance': 120,
                 'dhash': format(d, '016x'), 'phash': format(p, '016x')}, **values)


class SimilarityTests(unittest.TestCase):
    def setUp(self):
        self.settings = quality.policy()

    def test_independent_thresholds_and_conservative_decision(self):
        left = features('a')
        both = quality.compare_pair(left, features('b', 15, 63), self.settings)
        self.assertEqual((both['dhash_distance'], both['phash_distance']), (4, 6))
        self.assertTrue(both['match'])
        for d, p in ((31, 63), (15, 127)):
            row = quality.compare_pair(left, features('b', d, p), self.settings)
            self.assertFalse(row['match'])
            self.assertNotEqual(row['dhash_match'], row['phash_match'])

    def test_eligibility_gates_are_shared_and_symmetric(self):
        left = features('a')
        for changes, reason in (({'sha256': 'a'}, 'exact_duplicate'),
                                ({'luminance_std': 7.99}, 'low_texture'),
                                ({'width': 400}, 'aspect_ratio'),
                                ({'mean_luminance': 130.01}, 'luminance')):
            right = features('b', **changes)
            for a, b in ((left, right), (right, left)):
                pair = quality.compare_pair(a, b, self.settings)
                self.assertIn(reason, pair['exclusion_reasons'])
                self.assertFalse(pair['eligible'])
                self.assertFalse(pair['dhash_match'])
                self.assertFalse(pair['phash_match'])
        boundary = quality.compare_pair(left, features('b', mean_luminance=130,
                                                       luminance_std=8), self.settings)
        self.assertTrue(boundary['eligible'])

    def test_aspect_ratio_boundary_and_pair_order_are_stable(self):
        for dimensions in (((100, 100), (102, 100)),
                           ((100, 100), (100, 102)),
                           ((600, 400), (918, 600))):
            left = features('a', width=dimensions[0][0], height=dimensions[0][1])
            right = features('b', width=dimensions[1][0], height=dimensions[1][1])
            for a, b in ((left, right), (right, left)):
                with self.subTest(dimensions=dimensions, first=a['name']):
                    boundary = quality.compare_pair(a, b, self.settings)
                    self.assertTrue(boundary['eligible'])
                    self.assertTrue(boundary['match'])

        left, right = features('left', 15, 63), features('right', 0, 0)
        forward = quality.compare_pair(left, right, self.settings)
        reverse = quality.compare_pair(right, left, self.settings)
        for key in ('eligible', 'exclusion_reasons', 'dhash_distance',
                    'phash_distance', 'dhash_match', 'phash_match', 'match'):
            self.assertEqual(forward[key], reverse[key])

    def test_aspect_ratio_rejects_one_pixel_beyond_default_boundary(self):
        left = features('a', width=1_000_000, height=1)
        for width, expected in ((1_019_999, True), (1_020_000, True),
                                (1_020_001, False)):
            right = features('b', width=width, height=1)
            for a, b in ((left, right), (right, left)):
                with self.subTest(width=width, first=a['name']):
                    pair = quality.compare_pair(a, b, self.settings)
                    self.assertEqual(pair['eligible'], expected)
                    self.assertEqual(pair['exclusion_reasons'], [] if expected else ['aspect_ratio'])

        # Both images fit the pixel limit; the ratio is just over 2%.
        left = features('a', width=6209, height=6284)
        right = features('b', width=6310, height=6261)
        for a, b in ((left, right), (right, left)):
            pair = quality.compare_pair(a, b, self.settings)
            self.assertEqual(pair['exclusion_reasons'], ['aspect_ratio'])

    def test_all_pairs_counted_even_when_evidence_is_truncated(self):
        rows = [features(str(i)) for i in range(25)]
        report = similarity_report(rows, self.settings, display_limit=7)
        self.assertEqual(report['pair_counts']['compared_pairs'], 300)
        self.assertEqual(report['pair_counts']['near_duplicate_pairs'], 300)
        self.assertEqual(report['pair_counts']['displayed_pairs'], 7)
        self.assertTrue(report['pair_sampling']['truncated'])
        self.assertEqual(sum('near_duplicate' in row['flags'] for row in rows), 24)
        self.assertEqual(rows[-1]['similar_to'], '0')
        self.assertEqual(rows[-1]['hash_distance'], 0)

    def test_exact_copies_and_bad_images_are_not_near_candidates(self):
        original = features('a')
        exact = features('b', sha256='a', duplicate_of='a')
        bad = {'name': 'broken', 'decodable': False}
        report = similarity_report([original, exact, bad], self.settings)
        self.assertEqual(report['pairs'], [])
        self.assertEqual(report['pair_counts']['compared_pairs'], 0)

    def test_nearest_evidence_is_deterministic_and_matches_have_priority(self):
        a, b, c = features('a'), features('b', 255, 255), features('c')
        report = similarity_report([a, b, c], self.settings, display_limit=1)
        self.assertEqual((report['pairs'][0]['a'], report['pairs'][0]['b']), ('a', 'c'))
        self.assertTrue(report['pairs'][0]['match'])

    def test_each_image_records_its_closest_earlier_match(self):
        a, b, c = features('a'), features('b', 1, 1), features('c', 1, 1)
        similarity_report([a, b, c], self.settings)
        self.assertEqual(b['similar_to'], 'a')
        self.assertEqual((c['similar_to'], c['hash_distance'], c['phash_distance']),
                         ('b', 0, 0))

    def test_phash_matches_reference_library(self):
        import imagehash
        image = Image.linear_gradient('L').resize((320, 240))
        self.assertEqual(quality.phash(image), str(imagehash.phash(image)))
        data = io.BytesIO()
        image.save(data, format='PNG')
        measured = quality.metrics(data.getvalue(), self.settings)
        self.assertEqual(measured['phash'], quality.phash(image))
        self.assertEqual(len(measured['dhash']), 16)

    def test_strict_policy_validation(self):
        for value in (True, 6.0, -1, 17, float('nan'), '6'):
            with self.assertRaises(ValueError):
                quality.policy({'phash_distance': value})
        for value in (False, [], '', 0):
            with self.assertRaises(ValueError):
                quality.policy(value)
        self.assertEqual(quality.policy({'phash_distance': 0})['phash_distance'], 0)

    def test_version_policy_and_dependency_versions_change_fingerprint(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            Image.linear_gradient('L').resize((64, 48)).save(path / 'a.png')
            images = [{'name': 'a.png', 'path': 'a.png'}]
            first = quality.analyze(path, images)
            self.assertTrue(quality.report_is_current(first))
            again = quality.analyze(path, images)
            changed = quality.analyze(path, images, {'phash_distance': 5})
            self.assertEqual(first['fingerprint'], again['fingerprint'])
            self.assertNotEqual(first['fingerprint'], changed['fingerprint'])
            with patch.object(quality, 'VERSION', 'future'):
                self.assertFalse(quality.report_is_current(first))
                self.assertNotEqual(first['fingerprint'], quality.analyze(path, images)['fingerprint'])
            versions = dict(quality.runtime_versions(), scipy_version='different')
            with patch.object(quality, 'runtime_versions', return_value=versions):
                self.assertFalse(quality.report_is_current(first))
                self.assertNotEqual(first['fingerprint'], quality.analyze(path, images)['fingerprint'])
            old_evidence = dict(first, algorithms=dict(first['algorithms']))
            old_evidence['algorithms'].pop('image_evidence')
            self.assertFalse(quality.report_is_current(old_evidence))
            old_aspect = dict(first, algorithms=dict(first['algorithms']))
            old_aspect['algorithms'].pop('eligibility')
            self.assertFalse(quality.report_is_current(old_aspect))
            with patch('recon_workbench.image_similarity.algorithm_metadata',
                       return_value=old_aspect['algorithms']):
                self.assertNotEqual(first['fingerprint'], quality.analyze(path, images)['fingerprint'])


if __name__ == '__main__':
    unittest.main()
