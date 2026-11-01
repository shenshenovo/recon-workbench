"""Fast3R integration contracts, without requiring CUDA or model weights."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from recon_workbench import fast3r, pipeline, server, evaluate
from recon_workbench.pointcloud import cloud_packet

try:
    import numpy as np
except ImportError:
    np = None


class Fast3RTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.images = self.root / 'photos'
        self.images.mkdir()
        for name in ('a.jpg', 'b.png'):
            (self.images / name).write_bytes(b'input contract fixture')

    def tearDown(self):
        self.temp.cleanup()

    def test_two_views_and_limit_do_not_silently_drop_inputs(self):
        (self.images / '.hidden.jpg').touch()
        (self.images / 'depth.tiff').touch()
        self.assertEqual(fast3r.image_names(self.images, 2), ['a.jpg', 'b.png'])
        (self.images / 'c.jpeg').touch()
        with self.assertRaisesRegex(ValueError, '实际 3 张'):
            fast3r.image_names(self.images, 2)
        self.assertEqual(len(list(self.images.glob('*.jpeg'))), 1)

    def test_invalid_filter_and_resource_bounds_rejected(self):
        base = dict(model='model', max_views=8, max_points=100, confidence_percentile=50, seed=42)
        for change in ({'max_views': 1}, {'max_points': 0}, {'confidence_percentile': 100},
                       {'confidence_percentile': float('nan')}, {'seed': -1}, {'model': ''}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                fast3r.validate_options(argparse.Namespace(**dict(base, **change)))

    def test_existing_and_overlapping_output_preserve_source(self):
        existing = self.root / 'existing'
        existing.mkdir()
        keep = existing / 'keep'
        keep.write_text('keep')
        for output in (existing, self.images / 'nested', self.root):
            with contextlib.redirect_stderr(io.StringIO()):
                result = fast3r.main(['--images', str(self.images), '--output', str(output)])
            self.assertEqual(result, 1)
        self.assertEqual(keep.read_text(), 'keep')
        self.assertFalse((self.images / 'nested').exists())

    def test_ply_export_roundtrips_in_existing_web_viewer(self):
        path = self.root / 'pointcloud.ply'
        rows = [([1.5, -2, 3], [255, 128, 0]), ([-4, 5, 6], [0, 32, 255])]
        self.assertEqual(fast3r.write_cloud(path, iter(rows)), 2)
        packet = cloud_packet(path)
        size = struct.unpack('<I', packet[:4])[0]
        metadata = json.loads(packet[4:4+size])
        values = list(struct.iter_unpack('<ffffff', packet[4+size:]))
        self.assertEqual(metadata['original_points'], 2)
        self.assertEqual(values[0][:3], tuple(rows[0][0]))
        self.assertAlmostEqual(values[0][4], 128/255, places=6)

    def test_export_failure_does_not_publish_partial_cloud(self):
        path = self.root / 'pointcloud.ply'
        for rows in ([], [([float('nan'), 0, 0], [0, 0, 0])],
                     [([1, 2, 3], [255, 0, 0]), ([1, 2, float('inf')], [0, 0, 0])]):
            with self.assertRaises(ValueError):
                fast3r.write_cloud(path, iter(rows))
            self.assertEqual(list(self.root.glob('*.part')), [])
            self.assertFalse(path.exists())

    def test_fast3r_plan_skips_sfm_and_preserves_filter_configuration(self):
        args = pipeline.get_parser().parse_args(['--method', 'fast3r', '--images', str(self.images),
                    '--fast3r-size', '224', '--fast3r-max-views', '4', '--fast3r-confidence-percentile', '25'])
        plan = pipeline.build_plan(args, self.root / 'result')
        self.assertEqual(len(plan), 1)
        command = plan[0][1]
        self.assertIn('recon_workbench.fast3r', command)
        self.assertNotIn('recon_workbench.reconstruct', command)
        self.assertNotIn('--colmap', command)
        self.assertEqual(command[command.index('--image-size')+1], '224')
        self.assertEqual(command[command.index('--max-views')+1], '4')
        for kind in ('--dataset', '--sfm-output'):
            args = pipeline.get_parser().parse_args(['--method', 'fast3r', kind, str(self.images)])
            with self.assertRaises(ValueError):
                pipeline.build_plan(args, self.root / 'result')

    def test_web_fast3r_parameters_reach_route_validation(self):
        payload = {'method': 'fast3r', 'input_type': 'images', 'input': str(self.images),
                   'fast3r_size': 224, 'fast3r_max_views': 4, 'fast3r_precision': 'bf16'}
        with patch.object(server, 'project_path', return_value=self.images):
            command = server.job_arguments(payload, self.root / 'output')
            self.assertEqual(command[command.index('--fast3r-size')+1], '224')
            for change in ({'fast3r_max_views': 1}, {'fast3r_size': 1024}, {'fast3r_precision': 'bad'},
                           {'fast3r_confidence_percentile': float('nan')}, {'input_type': 'dataset'}):
                with self.subTest(change=change), self.assertRaises(ValueError):
                    server.job_arguments(dict(payload, **change), self.root / 'output')

    def test_evaluation_does_not_invent_sfm_or_render_metrics(self):
        folder = self.root / 'result'
        folder.mkdir()
        cloud = folder / 'pointcloud.ply'
        fast3r.write_cloud(cloud, [([1, 2, 3], [0, 0, 255])])
        (folder / 'config.json').write_text('{"max_image_size":1200}')
        (folder / 'summary.json').write_text(json.dumps({'method': 'fast3r', 'input_images': 2,
           'processed_views': 2, 'dense_points': 1, 'pointcloud': str(cloud), 'max_resolution': 512,
           'inference_seconds': 1.5, 'peak_gpu_allocated_bytes': 1000}))
        row = evaluate.collect_run(folder)
        for key in ('registered_images', 'registration_ratio', 'reprojection_error_px', 'psnr_db', 'ssim'):
            self.assertIsNone(row[key], key)
        self.assertEqual(row['max_resolution'], 512)
        self.assertEqual(row['dense_points'], 1)
        self.assertEqual(row['processed_views'], 2)

    def test_model_configuration_override_keeps_revision_and_source_file(self):
        snapshot = self.root / 'snapshots' / ('a' * 40)
        snapshot.mkdir(parents=True)
        config = snapshot / 'config.json'
        original = json.dumps({'encoder_args': {'attn_implementation':'flash_attention'},
                               'decoder_args': {'attn_implementation':'flash_attention'}, 'head_args': {}})
        config.write_text(original)
        calls = []
        class Model:
            @staticmethod
            def from_pretrained(source, **kwargs):
                calls.append((source, kwargs))
                return 'model fixture'
        hub = SimpleNamespace(hf_hub_download=lambda *args: str(config))
        with patch.dict('sys.modules', {'huggingface_hub': hub}):
            model, metadata = fast3r.load_model(Model, 'team/model-fixture')
        self.assertEqual(model, 'model fixture')
        self.assertEqual(calls[0][1]['revision'], 'a' * 40)
        self.assertEqual(calls[0][1]['encoder_args']['attn_implementation'], 'pytorch_auto')
        self.assertEqual(calls[0][1]['decoder_args']['attn_implementation'], 'pytorch_auto')
        self.assertEqual(metadata['model_revision'], 'a' * 40)
        self.assertEqual(config.read_text(), original)

    @unittest.skipUnless(np is not None, 'Optional NumPy needed to test real point-map array handling')
    def test_point_map_filter_retains_matching_colors_and_reports_sampling(self):
        class Tensor:
            def __init__(self, value): self.value = np.asarray(value, dtype=np.float32)
            def detach(self): return self
            def float(self): return self
            def cpu(self): return self
            def numpy(self): return self.value
        points = [[[[0, 0, 1], [1, 0, 1]], [[2, 0, 1], [float('nan'), 0, 1]]]]
        image = [[[[-1, 1], [-1, 1]], [[-1, -1], [1, 1]], [[1, -1], [-1, 1]]]]
        output = {'preds': [{'pts3d_in_other_view': Tensor(points), 'conf': Tensor([[[1, 2], [3, 4]]])}],
                  'views': [{'img': Tensor(image)}]}
        stats = {}
        rows = list(fast3r.prediction_rows(output, 50, 1, stats))
        self.assertEqual(rows, [([1.0, 0.0, 1.0], [255, 0, 0])])
        self.assertEqual(stats['predicted_points'], 4)
        self.assertEqual(stats['confidence_filtered_points'], 2)
        self.assertEqual(stats['per_view'][0]['exported_points'], 1)

    @unittest.skipUnless(np is not None, 'Optional NumPy needed to test real point-map array handling')
    def test_export_rejects_unaligned_point_maps(self):
        class Tensor:
            def __init__(self, shape): self.value = np.zeros(shape, dtype=np.float32)
            def detach(self): return self
            def float(self): return self
            def cpu(self): return self
            def numpy(self): return self.value
        output = {'preds': [{'pts3d_in_other_view': Tensor((1, 2, 2, 3)), 'conf': Tensor((1, 2, 2))}],
                  'views': [{'img': Tensor((1, 3, 3, 3))}]}
        with self.assertRaisesRegex(ValueError, '未对齐'):
            list(fast3r.prediction_rows(output, 50, 10, {}))


if __name__ == '__main__':
    unittest.main()
