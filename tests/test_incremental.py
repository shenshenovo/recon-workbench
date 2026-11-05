import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from recon_workbench.incremental import manifest, parent_info, prepare
from recon_workbench import pipeline
from recon_workbench.workspace import Workspace


class IncrementalTests(unittest.TestCase):
    def test_cli_dispatch_and_route_restriction(self):
        args = pipeline.get_parser().parse_args(['--method', 'sfm', '--images', '/new', '--base-sfm', '/base'])
        command = pipeline.build_plan(args, Path('/out'))[0][1]
        self.assertIn('--base-sfm', command)
        self.assertIn('/base', command)
        args.method = 'mvs'
        with self.assertRaises(ValueError):
            pipeline.build_plan(args, Path('/out'))

    def test_task_parent_survives_reopen(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = Workspace(root)
            parent = store.create_task('default', None, 'base', 'sfm', root / 'base', None, {})
            child = store.create_task('default', None, 'extended', 'sfm', root / 'child', None,
                                      {'parent_task_id': parent['id']})
            self.assertEqual(Workspace(root).task(child['id'])['parent_task_id'], parent['id'])

    def test_copy_isolated_and_reject_changed_or_duplicate_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / 'base'
            images = base / 'inputs'
            model = base / 'sparse/0'
            images.mkdir(parents=True)
            model.mkdir(parents=True)
            (images / 'a.jpg').write_bytes(b'old image')
            for name in ('cameras.bin', 'images.bin', 'points3D.bin'):
                (model / name).write_bytes(b'model')
            sqlite3.connect(base / 'database.db').close()
            (base / 'summary.json').write_text(json.dumps({'images_path': str(images), 'selected_model': str(model)}))
            (base / 'status.json').write_text(json.dumps({'status': 'completed'}))
            (base / 'input_manifest.json').write_text(json.dumps(manifest(images, ['a.jpg'])))
            additions = root / 'additions'
            additions.mkdir()
            (additions / 'a.jpg').write_bytes(b'new image')
            output = root / 'output'
            output.mkdir()
            target, saved, names = prepare(base, additions, ['a.jpg'], output)
            self.assertEqual(len(saved), 2)
            self.assertEqual((target / 'a.jpg').read_bytes(), b'old image')
            self.assertNotEqual(names[0], 'a.jpg')
            (output / 'summary.json').write_text(json.dumps({'images_path': str(target), 'selected_model': str(output / 'base_model')}))
            (output / 'status.json').write_text(json.dumps({'status': 'completed'}))
            (output / 'input_manifest.json').write_text(json.dumps(saved))
            (additions / 'b.jpg').write_bytes(b'third image')
            next_output = root / 'next-version'
            next_output.mkdir()
            _, next_saved, _ = prepare(output, additions, ['b.jpg'], next_output)
            self.assertEqual(len(next_saved), 3)
            self.assertTrue(all(next_saved[name] == value for name, value in saved.items()))
            (target / 'a.jpg').write_bytes(b'changed copy')
            self.assertEqual((images / 'a.jpg').read_bytes(), b'old image')
            (additions / 'a.jpg').write_bytes(b'old image')
            with self.assertRaises(ValueError):
                prepare(base, additions, ['a.jpg'], output)
            (images / 'a.jpg').write_bytes(b'changed original')
            with self.assertRaises(ValueError):
                parent_info(base)

    def test_missing_parent_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                parent_info(temporary)
