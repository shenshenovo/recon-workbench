import io
import http.client
import json
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch

from recon_workbench import server as app
from recon_workbench.workspace import Workspace


PNG = b'\x89PNG\r\n\x1a\n' + struct.pack('>I', 13) + b'IHDR' + struct.pack('>II', 10, 8)


def review_fixture(root, store):
    from PIL import Image
    source = root / 'photos'
    source.mkdir(exist_ok=True)
    for name, color in [('a.png', (80,120,150)), ('b.png', (100,130,160))]:
        Image.new('RGB', (64,48), color).save(source / name)
    image_set = store.register_set('default', source)
    state = store.analyze_images(image_set['id'])
    store.save_selection(image_set['id'], ['a.png','b.png'], state['report']['fingerprint'])
    return image_set


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.store = Workspace(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_project_upload_manifest_survives_restart(self):
        project = self.store.create_project('建筑')
        with patch.object(app, 'ROOT', self.root):
            uploads = app.Uploads(self.store)
            batch = uploads.create(project['id'], '门楼照片')
            uploads.receive(batch['id'], 'a.png', io.BytesIO(PNG), len(PNG))
            with self.assertRaises(ValueError):
                uploads.receive(batch['id'], 'bad.png', io.BytesIO(b'invalid'), 7)
        restarted = Workspace(self.root)
        restarted.rename_project(project['id'], '校园建筑')
        image_set = restarted.image_sets(project['id'])[0]
        self.assertEqual(image_set['count'], 1)
        self.assertEqual(image_set['images'][0]['width'], 10)
        self.assertEqual(restarted.project(project['id'])['name'], '校园建筑')
        self.assertEqual(len(restarted.image_sets('default')), 0)

    def new_task(self, name='experiment', status='running'):
        return self.store.create_task('default', None, name, 'sfm', self.root / 'outputs' / name,
                                      None, {'method':'sfm'}, status=status)

    def test_recover_incomplete_task_without_touching_files(self):
        task = self.new_task()
        folder = self.root / task['output']
        folder.mkdir(parents=True)
        original = '{"status":"running","current_stage":"01_sfm"}'
        (folder / 'status.json').write_text(original)
        restarted = Workspace(self.root)
        restarted.recover()
        row = restarted.task(task['id'])
        self.assertEqual(row['status'], 'interrupted')
        self.assertIn('未重新接管', row['error'])
        self.assertEqual((folder / 'status.json').read_text(), original)

    def test_recover_actual_completed_output_and_models(self):
        task = self.new_task()
        folder = self.root / task['output']
        folder.mkdir(parents=True)
        cloud = folder / 'sparse.ply'
        cloud.write_text('point cloud fixture')
        (folder / 'summary.json').write_text(json.dumps({'method':'sfm', 'pointcloud':str(cloud)}))
        (folder / 'status.json').write_text('{"status":"completed"}')
        self.store.recover()
        self.assertEqual(self.store.task(task['id'])['status'], 'completed')
        self.assertEqual(self.store.models(task['id'])[0]['path'], task['output'] + '/sparse.ply')
        self.store.sync_task(task['id'], 'completed')
        self.assertEqual(len(self.store.models(task['id'])), 1)

    def test_history_import_idempotent_includes_failures(self):
        for name, status in [('ok','completed'), ('bad','failed'), ('stop','cancelled'), ('old','running')]:
            folder = self.root / 'outputs' / name
            folder.mkdir(parents=True)
            (folder / 'status.json').write_text(json.dumps({'status':status,'error':'fixture' if status=='failed' else None}))
            if status == 'completed':
                (folder / 'summary.json').write_text('{"method":"sfm"}')
        self.store.import_history()
        self.store.import_history()
        rows = self.store.tasks('default')
        self.assertEqual(len(rows), 4)
        self.assertEqual({row['status'] for row in rows}, {'completed','failed','cancelled','interrupted'})

    def test_cross_project_set_and_outside_paths_rejected(self):
        source = self.root / 'photos'
        source.mkdir()
        image_set = self.store.register_set('default', source)
        other = self.store.create_project('other')
        with self.assertRaises(ValueError):
            self.store.register_set('default', '../outside')
        with self.assertRaises(ValueError):
            self.store.create_task(other['id'], image_set['id'], 'bad', 'sfm', 'outputs/bad', None, {})
        with patch.object(app, 'ROOT', self.root):
            with self.assertRaisesRegex(ValueError, '不属于'):
                app.Jobs(self.store).start({'project_id':other['id'], 'image_set_id':image_set['id']})

    def test_process_start_failure_is_saved(self):
        source = self.root / 'photos'
        source.mkdir()
        review_fixture(self.root, self.store)
        with patch.object(app, 'ROOT', self.root), patch.object(app.subprocess, 'Popen', side_effect=OSError('cannot start')):
            with self.assertRaises(OSError):
                app.Jobs(self.store).start({'method':'sfm','input_type':'images','input':'photos'})
        task = self.store.tasks()[0]
        self.assertEqual(task['status'], 'failed')
        self.assertEqual(task['error'], 'cannot start')

    def test_process_monitor_persists_without_browser_polling(self):
        (self.root / 'photos').mkdir()
        review_fixture(self.root, self.store)
        (self.root / 'main.py').write_text('''import json, pathlib, sys
output = pathlib.Path(sys.argv[sys.argv.index('--output')+1])
output.mkdir(parents=True)
(output/'status.json').write_text(json.dumps({'status':'completed','elapsed_seconds':0.1}))
(output/'summary.json').write_text(json.dumps({'method':'sfm'}))
print('fixture completed')
''')
        with patch.object(app, 'ROOT', self.root):
            jobs = app.Jobs(self.store)
            started = jobs.start({'name':'fixture','method':'sfm','input_type':'images','input':'photos'})
            jobs.worker.join(timeout=5)
            self.assertFalse(jobs.worker.is_alive())
            self.assertEqual(Workspace(self.root).task(started['task_id'])['status'], 'completed')
            detail = app.task_detail(self.store, started['task_id'])
            self.assertIn('fixture completed', detail['log_text'])
            self.assertTrue(detail['command'])

    def test_cancel_survives_restart(self):
        (self.root / 'photos').mkdir()
        review_fixture(self.root, self.store)
        (self.root / 'main.py').write_text('import time\nprint("ready", flush=True)\ntime.sleep(30)\n')
        with patch.object(app, 'ROOT', self.root):
            jobs = app.Jobs(self.store)
            started = jobs.start({'method':'sfm','input_type':'images','input':'photos'})
            try:
                jobs.cancel()
                jobs.worker.join(timeout=5)
                self.assertFalse(jobs.worker.is_alive())
                restarted = Workspace(self.root)
                restarted.recover()
                self.assertEqual(restarted.task(started['task_id'])['status'], 'cancelled')
            finally:
                jobs.close()


class PersistentApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        (self.root / 'web').mkdir()
        (self.root / 'web/index.html').write_text('__TOKEN__')
        self.patch = patch.object(app, 'ROOT', self.root)
        self.patch.start()
        self.start_server()

    def start_server(self):
        self.store = Workspace(self.root)
        self.store.recover()
        self.server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        self.server.store = self.store
        self.server.token = 'test-token'
        self.server.jobs = app.Jobs(self.store)
        self.server.uploads = app.Uploads(self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop_server(self):
        self.server.shutdown()
        self.server.jobs.close()
        self.server.server_close()
        self.thread.join(timeout=3)

    def tearDown(self):
        self.stop_server()
        self.patch.stop()
        self.temp.cleanup()

    def request(self, path, data=None, raw=None, token=True):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        headers = {'X-Local-Token':'test-token'} if token else {}
        body = raw if raw is not None else json.dumps(data).encode() if data is not None else None
        try:
            connection.request('POST' if body is not None else 'GET', path, body, headers)
            response = connection.getresponse()
            content = response.read()
            return response.status, json.loads(content) if response.headers.get('Content-Type','').startswith('application/json') else content
        finally:
            connection.close()

    def test_project_upload_and_preview_api_survive_restart(self):
        self.assertEqual(self.request('/api/projects', {'name':'blocked'}, token=False)[0], 403)
        code, project = self.request('/api/projects', {'name':'API 项目'})
        self.assertEqual(code, 200)
        code, batch = self.request('/api/upload-new', {'project_id':project['id'],'name':'照片'})
        self.assertEqual(code, 200)
        code, _ = self.request('/api/upload-file?id=' + batch['id'] + '&name=a.png', raw=PNG)
        self.assertEqual(code, 200)
        self.stop_server()
        self.start_server()
        code, detail = self.request('/api/project?id=' + project['id'])
        self.assertEqual(code, 200)
        self.assertEqual(detail['image_sets'][0]['count'], 1)
        code, content = self.request('/api/source-image?set=' + batch['image_set_id'] + '&name=a.png')
        self.assertEqual((code, content), (200, PNG))
        self.assertEqual(self.request('/api/source-image?set=' + batch['image_set_id'] + '&name=outside.png')[0], 400)
        self.request('/api/project-rename', {'id':project['id'], 'name':'新名称'})
        self.assertEqual(self.request('/api/project?id=' + project['id'])[1]['name'], '新名称')

    def test_failed_process_is_in_task_history_after_restart(self):
        photos = self.root / 'photos'
        photos.mkdir()
        review_fixture(self.root, self.store)
        (self.root / 'main.py').write_text('print("fixture failure", flush=True)\nraise SystemExit(1)\n')
        code, result = self.request('/api/start', {'method':'sfm', 'input_type':'images', 'input':'photos'})
        self.assertEqual(code, 200)
        self.server.jobs.worker.join(timeout=5)
        self.stop_server()
        self.start_server()
        rows = self.request('/api/tasks?project_id=default')[1]
        self.assertEqual(rows[0]['status'], 'failed')
        detail = self.request('/api/task?id=' + result['task_id'])[1]
        self.assertIn('fixture failure', detail['log_text'])
        self.assertEqual(self.request('/api/runs?project_id=default')[1], [])

    def test_quality_review_api_and_filtered_job_input(self):
        from PIL import Image
        photos = self.root / 'photos'
        photos.mkdir()
        Image.new('RGB',(64,48),(80,120,150)).save(photos / 'a.png')
        Image.new('RGB',(64,48),(100,130,160)).save(photos / 'b.png')
        (photos / 'z-copy.png').write_bytes((photos / 'a.png').read_bytes())
        code, image_set = self.request('/api/image-sets', {'path':'photos'})
        self.assertEqual(code, 200)
        payload = {'method':'sfm','image_set_id':image_set['id']}
        self.assertEqual(self.request('/api/start', payload)[0], 400)
        code, state = self.request('/api/quality-analyze', {'image_set_id':image_set['id']})
        self.assertEqual(code, 200)
        self.assertEqual(state['report']['counts']['exact_duplicate'],1)
        selection = {'image_set_id':image_set['id'],'fingerprint':state['report']['fingerprint'],'names':['a.png','b.png']}
        self.assertEqual(self.request('/api/image-selection',selection)[0],200)
        self.stop_server()
        self.start_server()
        state = self.request('/api/quality?image_set_id=' + image_set['id'])[1]
        self.assertEqual(state['selection']['names'],['a.png','b.png'])
        (self.root/'main.py').write_text('''import json, pathlib, sys
source=pathlib.Path(sys.argv[sys.argv.index('--images')+1])
assert sorted(p.name for p in source.iterdir())==['a.png','b.png']
output=pathlib.Path(sys.argv[sys.argv.index('--output')+1]); output.mkdir(parents=True)
(output/'status.json').write_text(json.dumps({'status':'completed'}))
(output/'summary.json').write_text(json.dumps({'method':'sfm','input_images':2}))
''')
        code, result = self.request('/api/start',payload)
        self.assertEqual(code,200)
        self.server.jobs.worker.join(timeout=5)
        task = self.request('/api/task?id='+result['task_id'])[1]
        self.assertEqual(task['status'],'completed')
        self.assertEqual(task['parameters']['selected_images'],['a.png','b.png'])
        self.assertNotEqual(task['parameters']['input'],'photos')

    def test_three_page_routes_and_static_assets(self):
        for name in ('projects.html','experiments.html'):
            (self.root/'web'/name).write_text(name+' __TOKEN__')
        for route in ('/reconstruct','/projects','/experiments'):
            code, page = self.request(route)
            self.assertEqual(code,200)
            self.assertIn(b'test-token',page)
        for name in ('styles.css','common.js','workbench.js','projects.js','experiments.js','quality.js','viewer.js'):
            (self.root/'web'/name).write_text('/* test asset */')
            self.assertEqual(self.request('/'+name)[0],200)
        self.assertEqual(self.request('/unknown.js')[0],404)


if __name__ == '__main__':
    unittest.main()
