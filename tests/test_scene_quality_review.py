import csv
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image, ImageDraw

from scripts.evaluate_scene_quality_review import evaluate


class SceneQualityReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'images').mkdir()
        for index, color in enumerate(((180, 40, 30), (175, 45, 35), (20, 80, 190))):
            image = Image.new('RGB', (120, 80), color)
            draw = ImageDraw.Draw(image)
            draw.rectangle((15 + index, 15, 45 + index, 60), fill=(240, 220, 80))
            image.save(self.root / 'images' / f'image-{index}.jpg', quality=95)
        self.candidates_path = self.root / 'review-candidates.json'
        self.labels_path = self.root / 'pair_labels_reviewed.csv'
        pairs = [
            {'id': 'P001', 'a': 'images/image-0.jpg', 'b': 'images/image-1.jpg',
             'source_a': 'image-0.jpg', 'source_b': 'image-1.jpg'},
            {'id': 'P002', 'a': 'images/image-0.jpg', 'b': 'images/image-2.jpg',
             'source_a': 'image-0.jpg', 'source_b': 'image-2.jpg'},
            {'id': 'P003', 'a': 'images/image-1.jpg', 'b': 'images/image-2.jpg',
             'source_a': 'image-1.jpg', 'source_b': 'image-2.jpg'},
        ]
        self.candidates_path.write_text(json.dumps({
            'scene': 'Test', 'source_image_count': 3, 'pairs': pairs,
        }), encoding='utf-8')
        with self.labels_path.open('w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=(
                'pair_id', 'split', 'image_a', 'image_b', 'label', 'reviewer', 'notes'))
            writer.writeheader()
            for pair, label in zip(pairs, ('near_duplicate', 'different', 'challenge')):
                writer.writerow({'pair_id': pair['id'], 'split': 'validation',
                                 'image_a': pair['source_a'], 'image_b': pair['source_b'],
                                 'label': label, 'reviewer': 'T', 'notes': ''})

    def tearDown(self):
        self.temp.cleanup()

    def test_reports_fixed_thresholds_and_excludes_challenge_pairs(self):
        report = evaluate(self.candidates_path, self.labels_path)
        self.assertEqual(report['label_counts'], {
            'near_duplicate': 1, 'different': 1, 'challenge': 1})
        for result in report['threshold_results'].values():
            combined = result['summary']['near_duplicate_methods']['combined']
            self.assertEqual(combined['pairs'], 2)
            self.assertEqual(combined['tp'] + combined['fp'] + combined['fn'] + combined['tn'], 2)
            self.assertEqual(result['summary']['challenge_pairs']['pairs'], 1)

    def test_rejects_changed_pair_identity(self):
        with self.labels_path.open(encoding='utf-8') as stream:
            rows = list(csv.DictReader(stream))
        rows[0]['image_b'] = 'image-2.jpg'
        with self.labels_path.open('w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        with self.assertRaisesRegex(ValueError, 'identity'):
            evaluate(self.candidates_path, self.labels_path)


if __name__ == '__main__':
    unittest.main()
