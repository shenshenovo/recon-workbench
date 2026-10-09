from pathlib import Path
import tempfile
import unittest

import main as app


class RouteTests(unittest.TestCase):
    def plan(self, *argv):
        return app.build_plan(app.get_parser().parse_args(list(argv)), Path('/tmp/new-task'))

    def test_existing_inputs_skip_sfm(self):
        for method in ('mvs', '3dgs'):
            plan = self.plan('--method', method, '--sfm-output', '/tmp/source')
            self.assertEqual(len(plan), 1)
            self.assertIn('/tmp/source', plan[0][1])
        plan = self.plan('--method', '3dgs', '--dataset', '/tmp/dataset')
        self.assertEqual(len(plan), 1)
        self.assertNotIn('--sfm-output', plan[0][1])

    def test_photo_route_passes_generated_sfm_to_next_stage(self):
        plan = self.plan('--method', '3dgs', '--images', '/tmp/photos', '--camera-model', 'PINHOLE')
        self.assertEqual(len(plan), 2)
        self.assertEqual(plan[0][2], Path('/tmp/new-task/sfm'))
        command = plan[1][1]
        self.assertEqual(command[command.index('--sfm-output') + 1], str(plan[0][2]))
        self.assertIn('PINHOLE', plan[0][1])

    def test_incompatible_source_route_rejected(self):
        for argv in [('--method', 'sfm', '--sfm-output', '/tmp/source'),
                     ('--method', 'mvs', '--dataset', '/tmp/dataset')]:
            with self.assertRaises(ValueError):
                self.plan(*argv)

    def test_dry_run_creates_no_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'dataset'
            source.mkdir()
            output = root / 'result'
            self.assertEqual(app.main(['--method', '3dgs', '--dataset', str(source),
                                      '--output', str(output), '--dry-run']), 0)
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
