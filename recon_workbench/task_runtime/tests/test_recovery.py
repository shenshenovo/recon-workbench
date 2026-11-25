"""恢复预检、独立 attempt、发布与故障边界的临时工作区测试。"""
import json
import http.client
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from recon_workbench import server
from recon_workbench.densify import run_dense
from recon_workbench.task_runtime.recovery import RecoveryManager, copy_verified_mvs_artifacts
from recon_workbench.task_runtime.recovery_worker import verify
from recon_workbench.task_runtime.repository import TaskRepository, RuntimeConflict
from recon_workbench.task_runtime.scheduler import Scheduler
from recon_workbench.task_runtime.resources import ResourceManager
from recon_workbench.task_runtime.snapshots import digest, file_hash
from recon_workbench.task_runtime.validators import capture_output_manifests
from recon_workbench.workspace import Workspace


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


def ply(path, mesh=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('ply\nformat ascii 1.0\nelement vertex 3\nproperty float x\nproperty float y\nproperty float z\n' +
                    ('element face 1\nproperty list uchar int vertex_indices\n' if mesh else '') +
                    'end_header\n0 0 0\n1 0 0\n0 1 0\n' + ('3 0 1 2\n' if mesh else ''))


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Workspace(self.root)
        self.repo = TaskRepository(self.store)
        self.manager = RecoveryManager(self.store, self.repo, server.job_arguments)

    def tearDown(self):
        self.temp.cleanup()

    def failed(self, method='mvs', completed='dense'):
        source = self.root / 'data/snapshot/images'
        source.mkdir(parents=True)
        (source / 'a.png').write_bytes(b'original photograph')
        identity = {'files': [{'name': 'a.png', 'sha256': file_hash(source / 'a.png')}]}
        fingerprint = digest(identity)
        manifest = self.root / 'data/snapshot/input.json'
        write_json(manifest, {'identity': identity, 'fingerprint': fingerprint})
        params = {'method': method, 'input_type': 'images', 'input': 'data/snapshot/images',
                  'input_manifest': 'data/snapshot/input.json', 'mvs_stage': 'textured', 'threads': 1}
        task = self.repo.prepare(project_id='default', image_set_id=None, name='scene', method=method,
                                 output='outputs/scene', log='outputs/.ui_logs/scene.log', parameters=params,
                                 command=[], request_fingerprint='request')[0]
        self.repo.publish(task['id'], params, [sys.executable, '-c', 'pass'], fingerprint)
        attempt = self.repo.claim_next()
        output = self.root / task['output']
        sfm = output / 'sfm'
        model = sfm / 'sparse/0'
        model.mkdir(parents=True)
        for name in ('cameras.bin', 'images.bin', 'points3D.bin'):
            (model / name).write_bytes(b'model')
        write_json(sfm / 'status.json', {'status': 'completed'})
        write_json(sfm / 'config.json', {'images': str(source)})
        write_json(sfm / 'summary.json', {'selected_model': str(model), 'input_images': 2})
        write_json(output / 'status.json', {'status': 'failed', 'stages': [
            {'name': '01_sfm', 'status': 'completed'}, {'name': '02_mvs', 'status': 'failed'}]})
        mvs = output / 'mvs'
        write_json(mvs / 'status.json', {'status': 'failed', 'dense_result': {'completed_stage': completed}})
        (mvs / 'dense').mkdir()
        for name in ('scene.mvs', 'dense.mvs', 'mesh.mvs'):
            (mvs / 'dense' / name).write_bytes(b'native-scene')
        ply(mvs / 'dense/dense.ply')
        ply(mvs / 'dense/dense_colmap.ply')
        ply(mvs / 'dense/mesh.ply', True)
        (mvs / 'undistorted/images').mkdir(parents=True)
        (mvs / 'undistorted/images/a.png').write_bytes(b'undistorted')
        capture_output_manifests(self.root, output)
        self.repo.finish(task['id'], attempt['current_attempt_id'], 'failed', 1)
        return self.repo.get(task['id'])

    def inspect(self, task):
        return self.manager.inspect(task['id'])

    def test_dense_and_mesh_boundaries_skip_verified_dependencies(self):
        task = self.failed()
        check = self.inspect(task)
        self.assertTrue(check['can_resume'])
        self.assertEqual([item['stage'] for item in check['reuse']], ['sfm', 'mvs.dense'])
        self.assertEqual(check['rerun'], ['mvs.mesh', 'mvs.textured'])
        mvs = self.root / task['output'] / 'mvs'
        state = json.loads((mvs / 'status.json').read_text())
        state['dense_result']['completed_stage'] = 'mesh'
        write_json(mvs / 'status.json', state)
        capture_output_manifests(self.root, self.root / task['output'])
        self.assertEqual(self.inspect(task)['rerun'], ['mvs.textured'])

    def test_corrupt_dependency_invalidates_downstream_and_input_blocks(self):
        task = self.failed()
        (self.root / task['output'] / 'sfm/sparse/0/cameras.bin').write_bytes(b'changed')
        check = self.inspect(task)
        self.assertEqual(check['reuse'], [])
        self.assertEqual(check['rerun'], ['sfm', 'mvs'])
        (self.root / task['parameters']['input'] / 'a.png').unlink()
        self.assertFalse(self.inspect(task)['can_resume'])

    def test_parameter_or_execution_version_changes_disable_reuse(self):
        task = self.failed()
        with self.repo.connect(write=True) as db:
            params = dict(task['parameters'], threads=2)
            db.execute('UPDATE tasks SET parameters=? WHERE id=?', (json.dumps(params), task['id']))
        self.assertEqual(self.inspect(task)['reuse'], [])
        with patch('recon_workbench.task_runtime.recovery.execution_fingerprint', return_value='changed'):
            self.assertEqual(self.inspect(task)['reuse'], [])

    def test_read_only_preflight_and_stale_plan_rejected(self):
        task = self.failed()
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        check = self.inspect(task)
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        (self.root / task['output'] / 'mvs/dense/dense.ply').write_bytes(b'bad')
        with self.assertRaises(RuntimeConflict):
            self.manager.resume(task['id'], check['plan_fingerprint'])

    def test_resume_idempotency_cancel_and_late_old_update(self):
        task = self.failed()
        check = self.inspect(task)
        with patch.object(server, 'ROOT', self.root):
            result = self.manager.resume(task['id'], check['plan_fingerprint'])
            replay = self.manager.resume(task['id'], check['plan_fingerprint'])
        self.assertEqual(result['attempt_id'], replay['attempt_id'])
        self.assertFalse(replay['created'])
        old = task['current_attempt_id']
        self.assertFalse(self.repo.finish(task['id'], old, 'completed', 0))
        self.repo.request_cancel(task['id'])
        self.assertEqual(self.repo.get(task['id'])['output'], task['output'])
        self.assertEqual(self.repo.attempt(old)['status'], 'failed')
        self.assertEqual(len(self.repo.attempts(task['id'])), 2)

    def test_concurrent_resume_has_one_attempt_and_joins_queue(self):
        task = self.failed()
        check = self.inspect(task)
        results, failures = [], []
        def resume():
            try:
                results.append(self.manager.resume(task['id'], check['plan_fingerprint']))
            except Exception as error:
                failures.append(error)
        with patch.object(server, 'ROOT', self.root):
            threads = [threading.Thread(target=resume) for _ in range(2)]
            for thread in threads: thread.start()
            for thread in threads: thread.join()
        self.assertEqual(failures, [])
        self.assertEqual(len({r['attempt_id'] for r in results}), 1)
        claimed = self.repo.claim_next()
        self.assertEqual(claimed['attempt_output'], self.repo.attempt(results[0]['attempt_id'])['output_path'])
        self.assertEqual(self.repo.get(task['id'])['output'], task['output'])
        verify(self.store, task['id'], results[0]['attempt_id'])
        (self.root / task['output'] / 'mvs/dense/dense.ply').unlink()
        with self.assertRaises(ValueError):
            verify(self.store, task['id'], results[0]['attempt_id'])

    def test_copy_includes_photos_and_does_not_mutate_source(self):
        task = self.failed()
        check = self.inspect(task)
        item = check['reuse'][1]
        source = self.root / item['source_path']
        before = {p: p.read_bytes() for p in source.rglob('*') if p.is_file()}
        destination = self.root / 'outputs/new/mvs'
        copy_verified_mvs_artifacts(source, destination, item['manifest_fingerprint'])
        self.assertEqual((destination / 'undistorted/images/a.png').read_bytes(), b'undistorted')
        self.assertEqual(before, {p: p.read_bytes() for p in source.rglob('*') if p.is_file()})

    def test_dense_resume_executes_only_mesh_and_texture(self):
        task = self.failed()
        item = self.inspect(task)['reuse'][1]
        output = self.root / 'outputs/recovered/mvs'
        copy_verified_mvs_artifacts(self.root / item['source_path'], output, item['manifest_fingerprint'])
        (output / 'logs').mkdir()
        calls = []
        def run(name, arguments, **kwargs):
            calls.append(name)
            if name == '09_mesh':
                (output / 'dense/mesh.mvs').write_bytes(b'mesh-scene')
                ply(output / 'dense/mesh.ply', True)
            elif name == '10_texture':
                (output / 'dense/textured.obj').write_text('mesh')
                (output / 'dense/textured.mtl').write_text('map_Kd texture.png')
                (output / 'dense/texture.png').write_bytes(b'texture')
            else:
                self.fail('恢复时不应执行该阶段：' + name)
        pipeline = SimpleNamespace(output=output, state={'stages': []}, run=run, save=lambda: None)
        args = SimpleNamespace(resume_stage='mesh', resume_from=self.root / item['source_path'],
                               resume_manifest_fingerprint=item['manifest_fingerprint'], stage='textured', threads=1)
        tools = {name: {'help': '', 'executable': name} for name in ('ReconstructMesh', 'TextureMesh')}
        result = run_dense(pipeline, None, None, args, tools)
        self.assertEqual(calls, ['09_mesh', '10_texture'])
        self.assertEqual(result['completed_stage'], 'textured')

    def test_publication_database_failure_rolls_back_then_restart_converges(self):
        task = self.failed()
        with patch.object(server, 'ROOT', self.root):
            result = self.manager.resume(task['id'], self.inspect(task)['plan_fingerprint'])
        claimed = self.repo.claim_next()
        output = self.root / claimed['attempt_output']
        cloud = output / 'cloud.ply'
        ply(cloud)
        write_json(output / 'summary.json', {'method': 'mvs', 'dense_pointcloud': str(cloud)})
        write_json(output / 'status.json', {'status': 'completed'})
        with patch.object(self.store, 'register_models', side_effect=sqlite3.OperationalError('disk full')):
            with self.assertRaises(sqlite3.OperationalError):
                self.repo.finish(task['id'], result['attempt_id'], 'completed', 0)
        self.assertEqual(self.repo.get(task['id'])['output'], task['output'])
        self.assertEqual(self.repo.get(task['id'])['status'], 'starting')
        scheduler = Scheduler(self.store)
        self.assertEqual(self.repo.get(task['id'])['status'], 'completed')
        self.assertEqual(self.repo.get(task['id'])['output'], claimed['attempt_output'])
        self.assertEqual(self.store.models(task['id'])[0]['path'], self.store.relative(cloud))
        self.store.import_history()
        self.assertEqual(len(self.store.tasks()), 1)
        scheduler.close()

    def test_training_reuses_prepared_dataset_but_restarts_training(self):
        task = self.failed(method='3dgs')
        dataset = self.root / task['output'] / '3dgs/dataset'
        write_json(dataset / 'dataset.json', {'status': 'prepared'})
        (dataset / 'sparse/0').mkdir(parents=True)
        for name in ('cameras.bin', 'images.bin', 'points3D.bin'):
            (dataset / 'sparse/0' / name).write_bytes(b'model')
        (dataset / 'images').mkdir()
        (dataset / 'images/a.png').write_bytes(b'photo')
        capture_output_manifests(self.root, dataset)
        check = self.inspect(task)
        self.assertEqual([i['stage'] for i in check['reuse']], ['sfm', '3dgs.prepare'])
        self.assertEqual(check['rerun'], ['3dgs.train'])

    def test_sfm_and_fast3r_restart_whole_stage(self):
        task = self.failed(method='fast3r')
        self.assertEqual(self.inspect(task)['rerun'], ['fast3r'])

    def test_sfm_restarts_whole_unit_without_internal_checkpoint(self):
        task = self.failed(method='sfm')
        self.assertEqual(self.inspect(task)['rerun'], ['sfm'])
        self.assertEqual(self.inspect(task)['reuse'], [])

    def test_recovery_waits_for_cpu_budget_and_cancel_beats_publication(self):
        task = self.failed()
        resources = ResourceManager(self.store, config={'cpu_slots': 1, 'max_running_tasks': 2})
        other = self.repo.prepare(project_id='default', image_set_id=None, name='other', method='sfm',
                                  output='outputs/other', log=None, parameters={}, command=['true'],
                                  request_fingerprint='other')[0]
        self.repo.publish(other['id'], {}, ['true'], 'input')
        occupying = self.repo.claim_next(resources)
        with patch.object(server, 'ROOT', self.root):
            resumed = self.manager.resume(task['id'], self.inspect(task)['plan_fingerprint'])
        self.assertIsNone(self.repo.claim_next(resources))
        self.assertEqual(self.repo.response(task['id'], resources=resources)['waiting_reason'], 'cpu_busy')
        self.repo.finish(other['id'], occupying['current_attempt_id'], 'failed', 1)
        self.repo.claim_next(resources)
        self.repo.request_cancel(task['id'])
        self.repo.finish(task['id'], resumed['attempt_id'], 'completed', 0)
        self.assertEqual(self.repo.get(task['id'])['status'], 'cancelled')
        self.assertEqual(self.repo.get(task['id'])['output'], task['output'])
        self.assertEqual(resources.snapshot(self.repo)['cpu']['allocated_slots'], 0)

    def test_missing_summary_is_not_published_after_restart(self):
        task = self.failed()
        with patch.object(server, 'ROOT', self.root):
            resumed = self.manager.resume(task['id'], self.inspect(task)['plan_fingerprint'])
        claimed = self.repo.claim_next()
        output = self.root / claimed['attempt_output']
        write_json(output / 'status.json', {'status': 'completed'})
        Scheduler(self.store).close()
        self.assertEqual(self.repo.get(task['id'])['status'], 'interrupted')
        self.assertEqual(self.repo.get(task['id'])['output'], task['output'])
        self.assertEqual(self.store.models(task['id']), [])

    def test_recovery_runs_through_real_worker_and_publishes_new_attempt(self):
        task = self.failed()
        (self.root / 'main.py').write_text('''import json, pathlib, sys
args=sys.argv
assert '--sfm-output' in args
assert '--mvs-resume-stage' in args
assert args[args.index('--mvs-resume-stage')+1]=='mesh'
output=pathlib.Path(args[args.index('--output')+1])
output.mkdir(parents=True)
cloud=output/'cloud.ply'
cloud.write_text('fixture cloud')
(output/'summary.json').write_text(json.dumps({'method':'mvs','dense_pointcloud':str(cloud)}))
(output/'status.json').write_text(json.dumps({'status':'completed'}))
''')
        with patch.object(server, 'ROOT', self.root):
            result = self.manager.resume(task['id'], self.inspect(task)['plan_fingerprint'])
        scheduler = Scheduler(self.store)
        package_root = str(Path(__file__).resolve().parents[3])
        with patch.dict(os.environ, {'PYTHONPATH': package_root}):
            scheduler.start()
            try:
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline and self.repo.get(task['id'])['status'] not in ('completed', 'failed'):
                    scheduler.wake.wait(.02)
                current = self.repo.get(task['id'])
                log = self.root / self.repo.attempt(result['attempt_id'])['log_path']
                self.assertEqual(current['status'], 'completed', log.read_text() if log.exists() else 'missing log')
                self.assertIn('/attempts/' + result['attempt_id'], current['output'])
                self.assertEqual(self.repo.attempt(task['current_attempt_id'])['status'], 'failed')
                self.assertEqual(len(self.store.models(task['id'])), 1)
            finally:
                scheduler.close()

    def test_resume_http_contract_token_conflict_and_history(self):
        task = self.failed()
        self.repo.set_persistent_dispatch_pause(True)
        with patch.object(server, 'ROOT', self.root):
            httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
            httpd.store = self.store
            httpd.token = 'test-token'
            httpd.jobs = server.Jobs(self.store)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            def post(path, payload, token='test-token'):
                connection = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=5)
                try:
                    connection.request('POST', path, json.dumps(payload), {'X-Local-Token': token})
                    response = connection.getresponse()
                    return response.status, json.loads(response.read())
                finally:
                    connection.close()
            try:
                self.assertEqual(post('/api/task-resume-check', {'task_id': task['id']}, 'bad')[0], 403)
                self.assertEqual(post('/api/task-resume-check', {'task_id': 'unknown'})[0], 404)
                code, checked = post('/api/task-resume-check', {'task_id': task['id']})
                self.assertEqual(code, 200)
                self.assertNotIn('_plan', checked)
                self.assertEqual(post('/api/task-resume', {'task_id': task['id'], 'plan_fingerprint': '0'*64})[0], 409)
                payload = {'task_id': task['id'], 'plan_fingerprint': checked['plan_fingerprint']}
                code, resumed = post('/api/task-resume', payload)
                self.assertEqual(code, 200)
                self.assertEqual(post('/api/task-resume', payload)[1]['attempt_id'], resumed['attempt_id'])
                detail = server.runtime_task_detail(self.store, task['id'], httpd.jobs)
                self.assertEqual(len(detail['attempts']), 2)
                self.assertEqual(detail['states'], [])
            finally:
                httpd.shutdown()
                httpd.jobs.close()
                httpd.server_close()
                thread.join(timeout=3)
