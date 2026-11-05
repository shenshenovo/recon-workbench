from pathlib import Path
import unittest
from unittest.mock import patch
from unittest.mock import Mock
import tempfile
import json

from recon_workbench import server as app


class InterfaceTests(unittest.TestCase):
    def test_nested_registered_task_is_available_as_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / 'outputs/comparison/incremental70'
            folder.mkdir(parents=True)
            (folder / 'summary.json').write_text(json.dumps({'method': 'sfm', 'points3D': 123}))
            store = Mock()
            store.tasks.return_value = [{'id': 'child', 'project_id': 'default',
                                         'output': 'outputs/comparison/incremental70', 'status': 'completed'}]
            with patch.object(app, 'ROOT', root):
                rows = app.records(store, 'default')
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['task_id'], 'child')
            self.assertEqual(rows[0]['sparse_points'], 123)

    def test_snapshot_includes_sfm_feature_and_matching_logs(self):
        with tempfile.TemporaryDirectory(dir=app.ROOT / 'outputs') as folder:
            output = Path(folder)
            logs = output / 'sfm/logs'
            logs.mkdir(parents=True)
            (logs / '01_features.log').write_text('Features: 8192')
            (logs / '02_matching.log').write_text('Matching block [1/2, 1/2]')
            jobs = app.Jobs()
            jobs.output = output
            jobs.log = output / 'main.log'
            jobs.process = Mock()
            jobs.process.poll.return_value = None
            result = jobs.snapshot()
            self.assertEqual(result['status'], 'running')
            self.assertIn('Features: 8192', result['log'])
            self.assertIn('Matching block [1/2, 1/2]', result['log'])

    def arguments(self, data):
        with patch.object(app, 'project_path', return_value=app.ROOT / 'web'):
            return app.job_arguments(data, app.ROOT / 'outputs/test-unused')
    def test_paths_stay_inside_project(self):
        with self.assertRaises(ValueError):
            app.project_path('../other-project')
        self.assertEqual(app.project_path('data'), app.ROOT / 'data')

    def test_invalid_route_and_numeric_values_rejected(self):
        for data in [{'method': 'mvs', 'input_type': 'dataset', 'input': 'data/brush/courtyard'},
                     {'input': 'data/brush/courtyard', 'steps': -1},
                     {'input': 'data/brush/courtyard', 'eval_split_every': 1}]:
            with self.assertRaises(ValueError):
                self.arguments(data)

    def test_input_path_is_single_argument(self):
        command = self.arguments({'input': 'data/brush/courtyard'})
        self.assertEqual(command[command.index('--dataset') + 1], str(app.ROOT / 'web'))
        self.assertIn('--eval-split-every', command)

    def test_web_camera_prior_is_validated_and_forwarded(self):
        payload = {'method': 'sfm', 'input_type': 'images', 'input': 'data/photos',
                   'camera_model': 'OPENCV_FISHEYE', 'camera_params': '1072,1068,1504,1000,0,0,0,0',
                   'single_camera': True}
        command = self.arguments(payload)
        self.assertEqual(command[command.index('--camera-params') + 1], payload['camera_params'])
        self.assertIn('--single-camera', command)
        for value in ['1072,1068,1504,1000', 'nan,1068,1504,1000,0,0,0,0', ['not a string']]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.arguments(dict(payload, camera_params=value))


if __name__ == '__main__':
    unittest.main()
