"""MVS using an external SfM must publish self-contained result files."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from recon_workbench import densify, reconstruct
from recon_workbench.workspace import Workspace


PLY = 'ply\nformat ascii 1.0\nelement vertex 1\nproperty float x\nproperty float y\nproperty float z\nend_header\n0 0 1\n'


class MvsPublicationTests(unittest.TestCase):
    def test_imported_sfm_without_source_ply_publishes_local_sparse_cloud(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / 'data/sfm-snapshot', root / 'outputs/mvs'
            photos, model = source / 'images', source / 'model'
            photos.mkdir(parents=True)
            model.mkdir()
            for name in ('cameras.bin', 'images.bin', 'points3D.bin'):
                (model / name).write_bytes(b'fixture')
            # Legacy summaries can reference a source PLY not copied into the snapshot.
            (source / 'config.json').write_text(json.dumps({'images': str(photos)}))
            (source / 'summary.json').write_text(json.dumps({'selected_model': str(model),
                'input_images': 2, 'registered_images': 2, 'points3D': 1,
                'pointcloud': str(root / 'older-task/missing.ply'), 'elapsed_seconds': 1}))

            def dense(pipeline, images, selected_model, args, tools):
                cloud = pipeline.output / 'dense.ply'
                cloud.write_text(PLY)
                return {'dense_pointcloud': str(cloud), 'dense_points': 1}

            def export(pipeline, name, arguments, **kwargs):
                self.assertEqual(name, '11_export_sparse')
                self.assertEqual(arguments[0], 'model_converter')
                self.assertEqual(Path(arguments[arguments.index('--input_path') + 1]), model)
                Path(arguments[arguments.index('--output_path') + 1]).write_text(PLY)

            with patch.object(densify.shutil, 'which', return_value=sys.executable), \
                    patch.object(reconstruct, 'read_help'), \
                    patch.object(densify, 'inspect_openmvs', return_value={}), \
                    patch.object(densify, 'run_dense', side_effect=dense), \
                    patch.object(reconstruct.Pipeline, 'run', new=export), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = densify.main(['--sfm-output', str(source), '--output', str(output), '--stage', 'dense'])
            self.assertEqual(code, 0)
            summary = json.loads((output / 'summary.json').read_text())
            self.assertEqual(Path(summary['sparse_pointcloud']), output / 'sparse.ply')
            store = Workspace(root)
            task = store.create_task('default', None, 'mvs', 'mvs', output, None, {})
            store.sync_task(task['id'], 'completed', 0)
            models = store.models(task_id=task['id'])
            self.assertEqual({row['path'] for row in models}, {'outputs/mvs/dense.ply', 'outputs/mvs/sparse.ply'})
