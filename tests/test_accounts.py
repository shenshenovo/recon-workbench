"""账号、权限与模型下载的回归测试。

分两层：``AccountTests`` 直接验证散列、会话与带归属的数据视图；``IsolationApiTests``
用真实 HTTP 验证两个账号的数据隔离、历史数据接管和下载权限。
"""
from contextlib import closing
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from recon_workbench import access
from recon_workbench import accounts
from recon_workbench import server as app
from recon_workbench.errors import AccessDenied
from recon_workbench.workspace import Workspace


PASSWORDS = 'correct horse battery'


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.store = Workspace(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_password_is_salted_and_verified(self):
        first = accounts.hash_password(PASSWORDS)
        second = accounts.hash_password(PASSWORDS)
        self.assertNotEqual(first, second)
        self.assertNotIn(PASSWORDS, first)
        self.assertTrue(accounts.verify_password(PASSWORDS, first))
        self.assertFalse(accounts.verify_password('wrong password', first))
        self.assertFalse(accounts.verify_password(PASSWORDS, 'md5$abc'))
        self.assertFalse(accounts.verify_password(PASSWORDS, None))

    def test_credentials_are_validated(self):
        for value in ['a', '', '  ', 'x' * 33, 'bad/name', 'has space']:
            with self.subTest(username=value), self.assertRaises(ValueError):
                accounts.username(value)
        for value in ['short', '', 'x' * 129, None]:
            with self.subTest(password=value), self.assertRaises(ValueError):
                accounts.password(value)
        self.assertEqual(accounts.username('  石孟千  '), '石孟千')

    def test_registration_rejects_duplicate_and_keeps_hash_only(self):
        self.store.register('alice', PASSWORDS)
        with self.assertRaises(ValueError):
            self.store.register('alice', PASSWORDS)
        with closing(sqlite3.connect(self.store.path)) as db:
            stored = db.execute('SELECT password_hash FROM users').fetchone()[0]
        self.assertNotIn(PASSWORDS, stored)
        self.assertTrue(stored.startswith('pbkdf2_sha256$'))
        self.assertEqual(self.store.count_users(), 1)

    def test_session_token_is_stored_as_digest(self):
        user, _ = self.store.register('alice', PASSWORDS)
        token = self.store.create_session(user['id'])
        self.assertEqual(self.store.session_user(token)['username'], 'alice')
        with closing(sqlite3.connect(self.store.path)) as db:
            rows = [row[0] for row in db.execute('SELECT token_hash FROM sessions')]
        self.assertEqual(len(rows), 1)
        self.assertNotIn(token, rows)
        self.assertEqual(rows[0], accounts.token_hash(token))
        self.assertTrue(self.store.delete_session(token))
        self.assertIsNone(self.store.session_user(token))

    def test_expired_session_is_rejected(self):
        user, _ = self.store.register('alice', PASSWORDS)
        token = 'expired-token'
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute('INSERT INTO sessions VALUES (?,?,?,?)',
                       (accounts.token_hash(token), user['id'], '2000-01-01T00:00:00+00:00',
                        '2000-01-02T00:00:00+00:00'))
            db.commit()
        self.assertIsNone(self.store.session_user(token))

    def test_first_account_claims_legacy_history(self):
        legacy = self.store.create_task('default', None, 'old-run', 'sfm',
                                       self.root / 'outputs/old-run', None, {'method': 'sfm'},
                                       status='completed')
        user, claimed = self.store.register('alice', PASSWORDS)
        self.assertEqual(claimed, 1)
        self.assertEqual(self.store.project('default', user['id'])['owner_id'], user['id'])
        self.assertEqual([row['id'] for row in self.store.tasks(owner=user['id'])], [legacy['id']])

        other, second_claim = self.store.register('bob', PASSWORDS)
        self.assertEqual(second_claim, 0)
        self.assertEqual(self.store.tasks(owner=other['id']), [])
        self.assertEqual(len(self.store.projects(other['id'])), 1)
        self.assertEqual(self.store.projects(other['id'])[0]['name'], '我的项目')

    def test_access_view_isolates_two_accounts(self):
        alice, _ = self.store.register('alice', PASSWORDS)
        bob, _ = self.store.register('bob', PASSWORDS)
        project = self.store.create_project('校园', alice['id'])
        task = self.store.create_task(project['id'], None, 'run', 'sfm',
                                     self.root / 'outputs/run', None, {'method': 'sfm'})
        self.store.sync_task(task['id'], 'failed')

        alice_view = access.Access(self.store, self.store.create_session(alice['id']))
        bob_view = access.Access(self.store, self.store.create_session(bob['id']))
        self.assertEqual({row['id'] for row in alice_view.projects()}, {project['id'], 'default'})
        self.assertEqual(alice_view.task(task['id'])['id'], task['id'])
        for call in (lambda: bob_view.project(project['id']), lambda: bob_view.task(task['id'])):
            with self.assertRaises(AccessDenied):
                call()
        self.assertEqual(bob_view.tasks(), [])

    def test_access_without_accounts_keeps_legacy_scope(self):
        self.store.create_project('历史')
        view = access.Access(self.store, None)
        self.assertFalse(view.login_required)
        self.assertFalse(view.signed_in)
        self.assertEqual({row['name'] for row in view.projects()}, {'历史', '默认项目 / 历史实验'})

    def test_login_throttle_blocks_repeated_failures(self):
        self.store.register('alice', PASSWORDS)
        throttle = accounts.Throttle(limit=3)
        for _ in range(3):
            self.assertIsNone(access.sign_in(self.store, 'alice', 'wrong password', throttle))
        with self.assertRaises(ValueError):
            access.sign_in(self.store, 'alice', PASSWORDS, throttle)
        throttle.reset('alice')
        self.assertEqual(access.sign_in(self.store, 'alice', PASSWORDS, throttle)[0]['username'], 'alice')


class IsolationApiTests(unittest.TestCase):
    FIXTURE = '''import json, pathlib, sys
output = pathlib.Path(sys.argv[sys.argv.index('--output')+1])
output.mkdir(parents=True)
cloud = output / 'sparse.ply'
cloud.write_text('ply fixture')
preview = output / 'gaussians'
preview.mkdir()
(preview / 'view.png').write_bytes(b'\\x89PNG\\r\\n\\x1a\\n')
(output / 'summary.json').write_text(json.dumps({'method': 'mvs', 'pointcloud': str(cloud),
                                                 'dense_points': 1}))
(output / 'status.json').write_text(json.dumps({'status': 'completed'}))
'''

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        (self.root / 'web').mkdir()
        for name in ('index.html', 'projects.html', 'experiments.html', 'login.html'):
            (self.root / 'web' / name).write_text(name + ' __TOKEN__')
        (self.root / 'web' / 'login.js').write_text('// login fixture')
        (self.root / 'main.py').write_text(self.FIXTURE)
        (self.root / 'sfm-run').mkdir()
        self.patch = patch.object(app, 'ROOT', self.root)
        self.patch.start()
        self.start_server()

    def tearDown(self):
        self.stop_server()
        self.patch.stop()
        self.temp.cleanup()

    def start_server(self):
        self.store = Workspace(self.root)
        self.store.import_history()
        self.store.recover()
        self.server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        self.server.store = self.store
        self.server.token = 'test-token'
        self.server.jobs = app.Jobs(self.store)
        self.server.uploads = app.Uploads(self.store)
        self.server.throttle = accounts.Throttle()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop_server(self):
        self.server.shutdown()
        self.server.jobs.close()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, path, data=None, cookie=None, token=True):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        headers = {}
        if token:
            headers['X-Local-Token'] = 'test-token'
        if cookie:
            headers['Cookie'] = cookie
        body = json.dumps(data).encode() if data is not None else None
        if body is not None:
            headers['Content-Type'] = 'application/json'
        try:
            connection.request('POST' if body is not None else 'GET', path, body, headers)
            response = connection.getresponse()
            content = response.read()
            kind = response.headers.get('Content-Type', '')
            payload = json.loads(content) if kind.startswith('application/json') else content
            return response.status, payload, response.getheader('Set-Cookie')
        finally:
            connection.close()

    def register(self, username):
        status, body, cookie = self.request('/api/auth/register',
                                           {'username': username, 'password': PASSWORDS})
        self.assertEqual(status, 200, body)
        return body, cookie

    def test_api_needs_login_once_an_account_exists(self):
        status, body, _ = self.request('/api/projects')
        self.assertEqual(status, 200)
        self.assertEqual([row['name'] for row in body], ['默认项目 / 历史实验'])
        self.assertFalse(self.request('/api/auth/me')[1]['auth_required'])

        self.register('alice')
        status, body, _ = self.request('/api/projects')
        self.assertEqual((status, body), (401, {'error': '请先登录。'}))
        status, _, _ = self.request('/reconstruct')
        self.assertEqual(status, 302)
        self.assertTrue(self.request('/api/auth/me')[1]['auth_required'])

    def test_register_login_logout_cycle(self):
        _, cookie = self.register('alice')
        status, body, _ = self.request('/api/auth/me', cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body['user']['username'], 'alice')
        self.assertTrue(cookie.startswith('rw_session='))

        _, _, cleared = self.request('/api/auth/logout', {}, cookie=cookie)
        self.assertIn('Max-Age=0', cleared)
        self.assertEqual(self.request('/api/projects', cookie=cookie)[0], 401)

        self.assertEqual(self.request('/api/auth/login',
                                      {'username': 'alice', 'password': 'wrong password'})[0], 401)
        status, body, again = self.request('/api/auth/login',
                                           {'username': 'alice', 'password': PASSWORDS})
        self.assertEqual((status, body['user']['username']), (200, 'alice'))
        self.assertEqual(self.request('/api/projects', cookie=again)[0], 200)

    def test_two_accounts_are_isolated_across_every_resource(self):
        alice, alice_cookie = self.register('alice')
        project = self.request('/api/projects', {'name': 'A 的项目'}, cookie=alice_cookie)[1]
        started = self.request('/api/start', {'project_id': project['id'], 'name': 'fixture', 'method': 'mvs',
                                              'input_type': 'sfm-output', 'input': 'sfm-run'},
                               cookie=alice_cookie)[1]
        self.server.jobs.worker.join(timeout=10)
        detail = self.request('/api/task?id=' + started['task_id'], cookie=alice_cookie)[1]
        self.assertEqual(detail['status'], 'completed')
        model = detail['models'][0]
        run = self.request('/api/runs?project_id=' + project['id'], cookie=alice_cookie)[1][0]

        status, content, _ = self.request('/api/model?id=' + model['id'], cookie=alice_cookie)
        self.assertEqual(status, 200)
        self.assertEqual(content, b'ply fixture')

        bob, bob_cookie = self.register('bob')
        self.assertEqual([row['name'] for row in self.request('/api/projects', cookie=bob_cookie)[1]],
                         ['我的项目'])
        blocked = [
            '/api/project?id=' + project['id'],
            '/api/tasks?project_id=' + project['id'],
            '/api/task?id=' + started['task_id'],
            '/api/source-image?set=' + detail['image_set_id'],
            '/api/model?id=' + model['id'],
            '/api/cloud?run=' + run['id'],
            '/api/previews?run=' + run['id'],
            '/api/image?path=' + str(run['id']) + '/gaussians/view.png',
        ]
        for path in blocked:
            with self.subTest(path=path):
                status, body, _ = self.request(path, cookie=bob_cookie)
                self.assertIn(status, (400, 403), body)
                self.assertTrue(body['error'])
        self.assertEqual(self.request('/api/runs', cookie=bob_cookie)[1], [])
        # 任务槽位只属于运行它的账号：B 看不到状态，也不能取消
        self.assertNotEqual(self.request('/api/job', cookie=bob_cookie)[1]['status'], 'running')
        self.assertEqual(self.request('/api/cancel', {}, cookie=bob_cookie)[0], 403)

        # A 自己的数据仍然可读，模型体积与路径不受隔离影响
        self.assertEqual(self.request('/api/tasks', cookie=alice_cookie)[1][0]['id'], started['task_id'])

    def test_history_imported_before_accounts_still_opens(self):
        folder = self.root / 'outputs' / 'legacy-run'
        folder.mkdir(parents=True)
        (folder / 'summary.json').write_text(json.dumps({'method': 'mvs'}))
        (folder / 'status.json').write_text(json.dumps({'status': 'completed'}))
        self.stop_server()
        self.start_server()

        alice, alice_cookie = self.register('alice')
        tasks = self.request('/api/tasks', cookie=alice_cookie)[1]
        self.assertEqual([row['name'] for row in tasks], ['legacy-run'])
        self.assertEqual(self.request('/api/project?id=default', cookie=alice_cookie)[1]['name'],
                         '默认项目 / 历史实验')

        bob, bob_cookie = self.register('bob')
        self.assertEqual(self.request('/api/tasks', cookie=bob_cookie)[1], [])
        self.assertEqual(self.request('/api/project?id=default', cookie=bob_cookie)[0], 403)

    def test_model_download_requires_ownership_and_real_file(self):
        _, alice_cookie = self.register('alice')
        project = self.request('/api/projects', {'name': 'A'}, cookie=alice_cookie)[1]
        started = self.request('/api/start', {'project_id': project['id'], 'name': 'fixture', 'method': 'mvs',
                                              'input_type': 'sfm-output', 'input': 'sfm-run'},
                               cookie=alice_cookie)[1]
        self.server.jobs.worker.join(timeout=10)
        model = self.request('/api/task?id=' + started['task_id'], cookie=alice_cookie)[1]['models'][0]
        (self.root / model['path']).unlink()
        status, body, _ = self.request('/api/model?id=' + model['id'], cookie=alice_cookie)
        self.assertEqual(status, 400)
        self.assertIn('不存在', body['error'])
        self.assertEqual(self.request('/api/model?id=unknown', cookie=alice_cookie)[0], 400)

    def test_login_page_and_assets_are_public(self):
        self.register('alice')
        status, page, _ = self.request('/login')
        self.assertEqual(status, 200)
        self.assertIn(b'test-token', page)
        self.assertEqual(self.request('/login.js')[0], 200)


if __name__ == '__main__':
    unittest.main()
