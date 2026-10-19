import contextlib
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

from recon_workbench import train_3dgs as app


def write_gaussian(path):
    names = ['x', 'y', 'z', 'opacity'] + ['scale_' + str(i) for i in range(3)]
    names += ['rot_' + str(i) for i in range(4)] + ['f_dc_' + str(i) for i in range(3)]
    header = 'ply\nformat binary_little_endian 1.0\nelement vertex 1\n'
    header += ''.join('property float ' + name + '\n' for name in names) + 'end_header\n'
    path.write_bytes(header.encode() + struct.pack('<' + 'f' * len(names), *([1.] * len(names))))


class TrainingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.dataset = self.root / 'dataset'
        self.dataset.mkdir()
        (self.dataset / 'dataset.json').write_text('{"status":"prepared"}')
        self.output = self.root / 'training'

    def run_main(self, export_step):
        def fake_run(pipeline, name, arguments, executable=None, env=None, terminal=False):
            if name == '03_train' and export_step:
                write_gaussian(pipeline.output / 'gaussians' / ('export_%s.ply' % export_step))
        with patch.object(app, 'inspect_brush', return_value=('/fake/brush', '0.3', '', 'total-steps')), \
             patch.object(app.shutil, 'which', return_value='/fake/colmap'), \
             patch.object(app, 'validate_dataset', return_value={'registered_images': 36, 'points3D': 13773,
                                                                'image_names': ['image_%02d.jpg' % i for i in range(36)]}), \
             patch.object(app.Pipeline, 'run', fake_run), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return app.main(['--dataset', str(self.dataset), '--output', str(self.output), '--steps', '10'])

    def test_completed_requires_final_gaussian_export(self):
        self.assertEqual(self.run_main(10), 0)
        summary = json.loads((self.output / 'summary.json').read_text())
        self.assertEqual(summary['splats'], 1)
        self.assertEqual(json.loads((self.output / 'status.json').read_text())['status'], 'completed')

    def test_missing_final_step_marks_failed_and_keeps_intermediate(self):
        self.assertEqual(self.run_main(5), 1)
        self.assertEqual(json.loads((self.output / 'status.json').read_text())['status'], 'failed')
        self.assertTrue((self.output / 'gaussians/export_5.ply').is_file())
        self.assertFalse((self.output / 'summary.json').exists())

    def test_existing_output_is_preserved(self):
        self.output.mkdir()
        sentinel = self.output / 'keep'
        sentinel.write_text('old result')
        self.assertEqual(self.run_main(10), 1)
        self.assertEqual(sentinel.read_text(), 'old result')
        self.assertFalse((self.output / 'config.json').exists())

    def test_truncated_gaussian_export_rejected(self):
        path = self.root / 'export.ply'
        write_gaussian(path)
        self.assertEqual(app.gaussian_ply_count(path), 1)
        path.write_bytes(path.read_bytes()[:-1])
        with self.assertRaisesRegex(ValueError, '截断'):
            app.gaussian_ply_count(path)

    @unittest.skipUnless(sys.platform != 'win32', 'POSIX pseudo-terminal')
    def test_terminal_only_progress_is_captured(self):
        self.output.mkdir()
        (self.output / 'logs').mkdir()
        pipeline = app.Pipeline(self.output, sys.executable, {})
        with contextlib.redirect_stdout(io.StringIO()):
            pipeline.run('progress', ['-c', 'import os; print("eval visible" if os.isatty(1) else "hidden")'], terminal=True)
        self.assertIn('eval visible', (self.output / 'logs/progress.log').read_text())


if __name__ == '__main__':
    unittest.main()
