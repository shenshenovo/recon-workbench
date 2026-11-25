import copy
import json
from pathlib import Path
import tempfile
import unittest

from scripts import evaluate_quality as evaluation
from scripts.make_quality_benchmark import make_benchmark
from scripts.quality_benchmark_data import make_synthetic_manifest


class QualityEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.path = make_synthetic_manifest(self.root / 'dataset', groups=2)
        self.document = json.loads(self.path.read_text(encoding='utf-8'))

    def tearDown(self):
        self.directory.cleanup()

    def manifest_with(self, document):
        path = self.path.parent / 'edited.json'
        path.write_text(json.dumps(document), encoding='utf-8')
        return evaluation.load_manifest(path)

    def photo_source(self, name='source', count=2):
        from PIL import Image
        source = self.root / name
        source.mkdir()
        for index in range(count):
            Image.new('RGB', (80, 60), (30 + index * 20, 80, 140)).save(
                source / ('v{}.png'.format(index)))
        return source

    def test_synthetic_manifest_has_source_grouped_splits_and_required_cases(self):
        manifest = evaluation.load_manifest(self.path)
        self.assertEqual(manifest['dataset_kind'], 'synthetic_smoke')
        self.assertEqual({row['kind'] for row in manifest['pairs']},
                         {'exact_copy', 'jpeg_reencode', 'resize', 'synthetic_parallax'})
        splits = {}
        for image in manifest['images'].values():
            splits.setdefault(image['source_group'], set()).add(image['split'])
        self.assertTrue(all(len(values) == 1 for values in splits.values()))
        self.assertEqual({pair['split'] for pair in manifest['pairs']}, {'calibration', 'validation'})

    def test_rejects_unknown_duplicate_reversed_and_conflicting_labels(self):
        for label in ('unknown', True, [], {}):
            document = copy.deepcopy(self.document)
            document['pairs'][0]['label'] = label
            with self.subTest(label=label), self.assertRaises(ValueError):
                self.manifest_with(document)
        for reverse, label in ((False, 'exact_duplicate'), (True, 'exact_duplicate'), (True, 'different')):
            document = copy.deepcopy(self.document)
            extra = dict(document['pairs'][0], label=label)
            if reverse:
                extra['a'], extra['b'] = extra['b'], extra['a']
            document['pairs'].append(extra)
            with self.subTest(reverse=reverse, label=label), self.assertRaises(ValueError):
                self.manifest_with(document)

    def test_rejects_nonstring_dataset_kind(self):
        for dataset_kind in ([], {}):
            document = copy.deepcopy(self.document)
            document['dataset_kind'] = dataset_kind
            with self.subTest(dataset_kind=dataset_kind), self.assertRaises(ValueError):
                self.manifest_with(document)

    def test_rejects_source_group_leakage_and_cross_split_pairs(self):
        document = copy.deepcopy(self.document)
        document['images'][1]['split'] = 'validation'
        with self.assertRaisesRegex(ValueError, 'source_group'):
            self.manifest_with(document)
        document = copy.deepcopy(self.document)
        document['pairs'][0]['b'] = document['images'][5]['name']
        with self.assertRaisesRegex(ValueError, 'pair cannot cross'):
            self.manifest_with(document)

    def test_rejects_unknown_images_self_pairs_and_unsafe_paths(self):
        for a, b in (('missing.png', self.document['images'][0]['name']), ('same', 'same')):
            document = copy.deepcopy(self.document)
            document['pairs'][0].update(a=a, b=b)
            with self.assertRaises(ValueError):
                self.manifest_with(document)
        for path in ('../outside.png', '/outside.png', 'C:/outside.png', 'images\\x.png'):
            document = copy.deepcopy(self.document)
            document['images'][0]['path'] = path
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.manifest_with(document)

    def test_confusion_counts_include_gated_negatives_and_exclude_exact(self):
        rows = [
            {'label': 'near_duplicate', 'kind': 'jpeg', 'dhash_match': True,
             'phash_match': True, 'match': True, 'exact_match': False},
            {'label': 'near_duplicate', 'kind': 'resize', 'dhash_match': True,
             'phash_match': False, 'match': False, 'exact_match': False},
            {'label': 'different', 'kind': 'parallax', 'dhash_match': True,
             'phash_match': False, 'match': False, 'exact_match': False},
            {'label': 'different', 'kind': 'gated', 'dhash_match': False,
             'phash_match': False, 'match': False, 'exact_match': False},
            {'label': 'exact_duplicate', 'kind': 'exact', 'dhash_match': False,
             'phash_match': False, 'match': False, 'exact_match': True}]
        summary = evaluation.summaries(rows)
        dhash = summary['near_duplicate_methods']['dhash']
        combined = summary['near_duplicate_methods']['combined']
        self.assertEqual((dhash['tp'], dhash['fp'], dhash['fn'], dhash['tn']), (2, 1, 0, 1))
        self.assertEqual((combined['tp'], combined['fp'], combined['fn'], combined['tn']), (1, 0, 1, 2))
        self.assertEqual(combined['pairs'], 4)
        self.assertEqual(summary['exact_sha256']['tp'], 1)
        self.assertIsNone(evaluation.classification_counts([], 'match')['precision'])

    def test_real_comparison_smoke_records_all_methods_and_separate_timing(self):
        report = evaluation.evaluate(evaluation.load_manifest(self.path), repeats=2)
        self.assertEqual(report['images'], 5)
        self.assertEqual(len(report['pairs']), 4)
        self.assertEqual(report['summary']['exact_sha256']['tp'], 1)
        self.assertEqual(set(report['summary']['near_duplicate_methods']), {'dhash', 'phash', 'combined'})
        for row in report['pairs']:
            self.assertIn('dhash_distance', row)
            self.assertIn('phash_distance', row)
            self.assertEqual(row['match'], row['dhash_match'] and row['phash_match'])
        for key in ('shared_feature_extraction', 'shared_pair_comparison'):
            self.assertEqual(len(report['timing'][key]['runs_seconds']), 2)
            self.assertGreaterEqual(report['timing'][key]['median_seconds'], 0)
        for key in ('dhash', 'phash'):
            self.assertEqual(len(report['timing']['hash_only'][key]['runs_seconds']), 2)
        json.dumps(report, allow_nan=False)

    def test_rejects_exact_label_that_disagrees_with_file_bytes(self):
        document = copy.deepcopy(self.document)
        document['pairs'][4]['label'] = 'near_duplicate'
        manifest = self.manifest_with(document)
        with self.assertRaisesRegex(ValueError, 'SHA-256 contradicts'):
            evaluation.evaluate(manifest, repeats=1)

    def test_rejects_bad_repeats_and_missing_split_pairs(self):
        manifest = evaluation.load_manifest(self.path)
        for repeats in (0, 51, 1.5, True):
            with self.subTest(repeats=repeats), self.assertRaises(ValueError):
                evaluation.evaluate(manifest, repeats=repeats)
        manifest['pairs'] = [row for row in manifest['pairs'] if row['split'] == 'calibration']
        with self.assertRaisesRegex(ValueError, 'No pairs'):
            evaluation.evaluate(manifest, repeats=1)

    def test_cli_writes_report_and_refuses_overwrite(self):
        output = self.root / 'report'
        self.assertEqual(evaluation.main(['--synthetic-smoke', '--output', str(output), '--repeats', '1']), 0)
        self.assertTrue((output / 'quality-evaluation.json').is_file())
        with self.assertRaises(SystemExit):
            evaluation.main(['--synthetic-smoke', '--output', str(output)])

    def test_real_source_transform_builder_keeps_challenges_out_of_main_metrics(self):
        source = self.photo_source()
        output = self.root / 'generated'
        manifest_path = make_benchmark(output, ['scene=' + str(source)], limit=2)
        manifest = evaluation.load_manifest(manifest_path)
        self.assertEqual(manifest['dataset_kind'], 'generated_transforms')
        self.assertIn('challenge', {row['label'] for row in manifest['pairs']})
        self.assertNotIn('different', {row['label'] for row in manifest['pairs']})
        report = evaluation.evaluate(manifest, repeats=1, split='calibration')
        self.assertEqual(report['summary']['near_duplicate_methods']['combined']['pairs'], 4)
        self.assertIsNone(report['summary']['near_duplicate_methods']['combined']['false_positive_rate'])
        self.assertEqual(report['summary']['challenge_pairs']['pairs'], 4)
        self.assertEqual(report['summary']['challenge_pairs']['by_kind']['crop']['pairs'], 2)

    def test_real_source_builder_validates_boundaries_and_reviewed_negatives(self):
        source = self.photo_source()
        with self.assertRaises(ValueError):
            make_benchmark(source / 'generated', ['scene=' + str(source)], limit=1)
        with self.assertRaises(ValueError):
            make_benchmark(self.root / 'unsafe', ['bad/name=' + str(source)], limit=1)
        reviewed = self.root / 'reviewed.json'
        reviewed.write_text(json.dumps([{'a': 'scene_000.png', 'b': 'scene_001.png',
                                         'kind': 'human_view_review'}]), encoding='utf-8')
        manifest_path = make_benchmark(self.root / 'reviewed', ['scene=' + str(source)],
                                       limit=2, negative_pairs=reviewed)
        manifest = evaluation.load_manifest(manifest_path)
        self.assertIn('human_view_review', {row['kind'] for row in manifest['pairs']})
        report = evaluation.evaluate(manifest, repeats=1, split='calibration')
        self.assertEqual(report['summary']['near_duplicate_methods']['combined']['by_kind']
                         ['human_view_review']['pairs'], 1)

    def test_split_by_source_keeps_each_original_group_in_one_split(self):
        source = self.photo_source(count=4)
        manifest_path = make_benchmark(self.root / 'split', ['scene=' + str(source)],
                                       limit=4, split_by_source=True)
        manifest = evaluation.load_manifest(manifest_path)
        groups = {}
        for image in manifest['images'].values():
            groups.setdefault(image['source_group'], set()).add(image['split'])
        self.assertEqual(len(groups), 4)
        self.assertTrue(all(len(splits) == 1 for splits in groups.values()))
        self.assertEqual({next(iter(splits)) for splits in groups.values()},
                         {'calibration', 'validation'})

    def test_transform_manifest_records_uniform_selection_and_source_provenance(self):
        source = self.photo_source(count=5)
        path = make_benchmark(self.root / 'provenance', ['scene=' + str(source)], limit=3)
        document = json.loads(path.read_text(encoding='utf-8'))
        originals = [image for image in document['images'] if image['transform']['kind'] == 'identity']
        self.assertEqual([row['source']['relative_path'] for row in originals],
                         ['v0.png', 'v2.png', 'v4.png'])
        for original in originals:
            self.assertEqual((path.parent / original['path']).read_bytes(),
                             Path(original['source']['path']).read_bytes())
            self.assertEqual(original['sha256'], original['source']['sha256'])
        for row in document['images']:
            self.assertEqual(row['scene'], 'scene')
            self.assertIn('kind', row['transform'])
            self.assertEqual(len(row['sha256']), 64)

    def test_builder_rejects_invalid_limit_roots_splits_and_cross_split_duplicates(self):
        source = self.photo_source()
        cases = [
            {'sources': ['scene=' + str(source)], 'limit': 0},
            {'sources': ['scene=' + str(source)], 'limit': True},
            {'sources': ['scene=' + str(source)], 'limit': 84},
            {'sources': ['scene=' + str(source)], 'split_overrides': {'scene': []}},
            {'sources': ['scene=' + str(source)], 'split_overrides': {'missing': 'validation'}},
            {'sources': ['scene=' + str(source), 'SCENE=' + str(source)]},
            {'sources': ['scene=' + str(source), 'other=' + str(source)]},
        ]
        for index, options in enumerate(cases):
            output = self.root / ('invalid' + str(index))
            with self.subTest(options=options), self.assertRaises(ValueError):
                make_benchmark(output, **options)
            self.assertFalse(output.exists())
        (source / 'v1.png').write_bytes((source / 'v0.png').read_bytes())
        with self.assertRaisesRegex(ValueError, 'identical image bytes'):
            make_benchmark(self.root / 'duplicates', ['scene=' + str(source)], split_by_source=True)

    def test_builder_rejects_unreviewed_pair_shapes_before_creating_output(self):
        source = self.photo_source()
        reviewed = self.root / 'reviewed.json'
        cases = [
            [{'a': 'missing', 'b': 'scene_000.png'}],
            [{'a': 'scene_000.png', 'b': 'scene_000.png'}],
            [{'a': 'scene_000.png', 'b': 'scene_000_exact.png'}],
            [{'a': 'scene_000.png', 'b': 'scene_001.png', 'kind': []}],
            [{'a': 'scene_000.png', 'b': 'scene_001.png'},
             {'a': 'scene_001.png', 'b': 'scene_000.png'}],
        ]
        for index, pairs in enumerate(cases):
            reviewed.write_text(json.dumps(pairs), encoding='utf-8')
            output = self.root / ('invalid-reviewed' + str(index))
            with self.subTest(pairs=pairs), self.assertRaises(ValueError):
                make_benchmark(output, ['scene=' + str(source)], negative_pairs=reviewed)
            self.assertFalse(output.exists())
        reviewed.write_text(json.dumps([{'a': 'scene_000.png', 'b': 'scene_001.png'}]),
                            encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'cross calibration'):
            make_benchmark(self.root / 'cross', ['scene=' + str(source)], negative_pairs=reviewed,
                           split_by_source=True)

    def test_manifest_hash_must_match_before_evaluation(self):
        document = copy.deepcopy(self.document)
        image = next(row for row in document['images'] if row['split'] == 'validation')
        image['sha256'] = '0' * 64
        manifest = self.manifest_with(document)
        with self.assertRaisesRegex(ValueError, 'disagree with the manifest sha256'):
            evaluation.evaluate(manifest, repeats=1, split='validation')
        image = next(row for row in document['images'] if row['split'] == 'calibration')
        for digest in ([], {}, True, 'ABC', 'a' * 63):
            image['sha256'] = digest
            with self.subTest(digest=digest), self.assertRaises(ValueError):
                self.manifest_with(document)

    def test_manifest_hash_is_optional_but_report_records_computed_hash(self):
        document = copy.deepcopy(self.document)
        for image in document['images']:
            image.pop('sha256', None)
        manifest = self.manifest_with(document)
        report = evaluation.evaluate(manifest, repeats=1, split='validation')
        expected = {
            name
            for pair in manifest['pairs']
            if pair['split'] == 'validation'
            for name in (pair['a'], pair['b'])
        }
        self.assertEqual(set(report['image_sha256']), expected)
        self.assertTrue(all(len(value) == 64 for value in report['image_sha256'].values()))

    def test_challenges_are_unclassified_even_when_the_bytes_happen_to_match(self):
        document = copy.deepcopy(self.document)
        document['dataset_kind'] = 'human_labeled'
        for pair in document['pairs']:
            pair['label'] = 'challenge'
        report = evaluation.evaluate(self.manifest_with(document), repeats=1)
        summary = report['summary']
        self.assertEqual(summary['exact_sha256']['pairs'], 0)
        self.assertEqual(summary['challenge_pairs']['pairs'], 4)
        for method in summary['near_duplicate_methods'].values():
            self.assertEqual(method['pairs'], 0)
            self.assertIsNone(method['precision'])


if __name__ == '__main__':
    unittest.main()
