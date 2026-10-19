"""Check import and subprocess boundaries after moving code into a package."""
import contextlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from recon_workbench.paths import PROJECT_ROOT
from recon_workbench.reconstruct import Pipeline


class EntrypointTests(unittest.TestCase):
    def test_root_launchers_work_from_another_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            for script in ('app.py', 'main.py'):
                with self.subTest(script=script):
                    result = subprocess.run([sys.executable, str(PROJECT_ROOT / script), '--help'],
                                            cwd=directory, capture_output=True, text=True, timeout=15)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('usage:', result.stdout)

    def test_module_cli_imports(self):
        modules = ('reconstruct', 'densify', 'prepare_brush', 'train_3dgs', 'evaluate', 'ply_compatible')
        for module in ['recon_workbench.' + name for name in modules] + ['scripts.make_quality_demo']:
            with self.subTest(module=module):
                result = subprocess.run([sys.executable, '-m', module, '--help'], cwd=PROJECT_ROOT,
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_pipeline_module_child_uses_repository_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / 'logs').mkdir()
            pipeline = Pipeline(output, sys.executable, {})
            with contextlib.redirect_stdout(io.StringIO()):
                pipeline.run('module', ['-m', 'recon_workbench.prepare_brush', '--help'], cwd=PROJECT_ROOT)
            self.assertEqual(pipeline.state['stages'][0]['status'], 'completed')
            self.assertEqual(pipeline.state['stages'][0]['cwd'], str(PROJECT_ROOT))
            self.assertIn('--sfm-output', (output / 'logs/module.log').read_text())


if __name__ == '__main__':
    unittest.main()
