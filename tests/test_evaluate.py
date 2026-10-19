import json
from pathlib import Path
import tempfile
import unittest

from recon_workbench import evaluate as app


class EvaluationTests(unittest.TestCase):
    def test_terminal_duplicates_and_scientific_notation(self):
        text = '\x1b[32mEval iter 10: PSNR 2.5e1, ssim 0.9\rEval iter 10: PSNR 25, ssim 0.9\nEval iter 20: PSNR 27, ssim 0.95'
        curve = app.parse_brush_metrics(text)
        self.assertEqual(len(curve), 2)
        self.assertEqual(curve[0], {'step': 10, 'psnr_db': 25, 'ssim': 0.9})

    def test_split_matches_brush_zero_based_rule(self):
        with tempfile.TemporaryDirectory() as directory:
            split = app.make_split(directory, ['%02d.jpg' % i for i in reversed(range(36))], 8, 1200, 42)
            self.assertEqual(split['eval_images'], ['00.jpg', '08.jpg', '16.jpg', '24.jpg', '32.jpg'])
            self.assertEqual(len(split['train_images']), 31)

    def test_no_holdout_does_not_report_quality(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'summary.json').write_text(json.dumps({'method': '3dgs', 'evaluation_enabled': False}))
            (root / 'logs').mkdir()
            (root / 'logs/03_train.log').write_text('Eval iter 10: PSNR 50, ssim 1')
            row = app.collect_run(root)
            self.assertIsNone(row['psnr_db'])
            self.assertIsNone(row['comparison_group'])

    def test_report_keeps_legacy_sfm_metrics_and_rejects_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / 'sfm'
            run.mkdir()
            (run / 'summary.json').write_text(json.dumps({'input_images': 10, 'registered_images': 8,
                'mean_point_reprojection_error_px': 0.5, 'points3D': 100}))
            output = root / 'report'
            report = app.export_report([run], output)
            row = report['runs'][0]
            self.assertEqual(row['registration_ratio'], 0.8)
            self.assertEqual(row['reprojection_error_px'], 0.5)
            self.assertIsNone(row['psnr_db'])
            self.assertTrue((output / 'comparison.csv').is_file())
            with self.assertRaises(FileExistsError):
                app.export_report([run], output)


if __name__ == '__main__':
    unittest.main()
