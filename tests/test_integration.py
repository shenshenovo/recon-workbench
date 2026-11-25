"""Cross-PR authorization, queue and incremental-version regression tests."""
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

from PIL import Image

from recon_workbench.incremental import manifest, parent_fingerprint
from recon_workbench.workspace import Workspace
from tests import test_accounts as account_tests


class IntegrationTests(unittest.TestCase):
    FIXTURE = account_tests.IsolationApiTests.FIXTURE
    setUp = account_tests.IsolationApiTests.setUp
    tearDown = account_tests.IsolationApiTests.tearDown
    start_server = account_tests.IsolationApiTests.start_server
    stop_server = account_tests.IsolationApiTests.stop_server
    request = account_tests.IsolationApiTests.request
    register = account_tests.IsolationApiTests.register

    def accounts(self):
        alice, a_cookie = self.register('alice_integration')
        bob, b_cookie = self.register('bob_integration')
        return alice, a_cookie, bob, b_cookie

    def image_set(self, user, cookie, folder):
        directory = self.root / folder
        directory.mkdir(parents=True)
        for index, color in enumerate(((21, 60, 110), (110, 60, 21))):
            Image.new('RGB', (20, 16), color).save(directory / f'{index}.png')
        code, result, _ = self.request('/api/image-sets', {
            'project_id': user['projects'][0]['id'], 'path': folder, 'input_type': 'images'}, cookie=cookie)
        self.assertEqual(code, 200, result)
        return result

    def test_foreign_directory_cannot_be_rebound_or_used_for_submission(self):
        alice, a_cookie, bob, b_cookie = self.accounts()
        images = self.image_set(alice, a_cookie, 'data/uploads/private-alice')
        own_project = bob['projects'][0]['id']
        self.assertEqual(self.request('/api/source-image?set=' + images['id'] + '&name=0.png', cookie=b_cookie)[0], 403)
        for folder in ('data/uploads/private-alice', 'data/uploads'):
            code, _, _ = self.request('/api/image-sets', {
                'project_id': own_project, 'path': folder, 'input_type': 'images'}, cookie=b_cookie)
            self.assertEqual(code, 403)
        code, _, _ = self.request('/api/start', {'project_id': own_project, 'method': 'sfm',
            'input_type': 'images', 'input': 'data/uploads/private-alice'}, cookie=b_cookie)
        self.assertEqual(code, 403)
        listing = self.request('/api/folders?path=data/uploads', cookie=b_cookie)
        self.assertEqual(listing[0], 200)
        self.assertEqual(listing[1]['folders'], [])

    def test_foreign_file_symlink_cannot_be_imported(self):
        alice, a_cookie, bob, b_cookie = self.accounts()
        self.image_set(alice, a_cookie, 'data/uploads/private-alice')
        alias = self.root / 'data/uploads/bob-alias'
        alias.mkdir()
        (alias / 'alias.png').symlink_to(self.root / 'data/uploads/private-alice/0.png')
        code, _, _ = self.request('/api/image-sets', {'project_id': bob['projects'][0]['id'],
            'path': 'data/uploads/bob-alias', 'input_type': 'images'}, cookie=b_cookie)
        self.assertEqual(code, 403)

    def queued(self, user, name):
        jobs = self.server.jobs
        jobs.scheduler.blocked_reason = 'integration_test_pause'
        task, _ = jobs.repository.prepare(project_id=user['projects'][0]['id'], image_set_id=None,
            name=name, method='sfm', output='outputs/' + name, log=None, parameters={},
            command=[sys.executable, '-c', 'raise SystemExit(0)'], request_fingerprint=name)
        jobs.repository.publish(task['id'], {}, task['command'], 'fixture-input')
        return task

    def test_queue_status_cancel_resume_and_details_are_account_scoped(self):
        alice, a_cookie, bob, b_cookie = self.accounts()
        private = self.queued(alice, 'alice-private-task')
        own = self.queued(bob, 'bob-own-task')
        queue = self.request('/api/queue', cookie=b_cookie)
        self.assertEqual(queue[0], 200)
        self.assertEqual([row['id'] for row in queue[1]['queued']], [own['id']])
        self.assertEqual(self.request('/api/job', cookie=b_cookie)[1]['task_id'], own['id'])
        for endpoint in ('/api/job?task_id=', '/api/task?id='):
            self.assertEqual(self.request(endpoint + private['id'], cookie=b_cookie)[0], 403)
        for endpoint, payload in (
            ('/api/cancel', {'task_id': private['id']}),
            ('/api/task-resume-check', {'task_id': private['id']}),
            ('/api/task-resume', {'task_id': private['id'], 'plan_fingerprint': '0' * 64}),
            ('/api/queue-resume', {}),
        ):
            self.assertEqual(self.request(endpoint, payload, cookie=b_cookie)[0], 403)
        self.assertEqual(self.request('/api/cancel', {'task_id': own['id']}, cookie=b_cookie)[0], 200)
        self.assertEqual(self.store.task(private['id'])['status'], 'queued')
        self.assertEqual(self.store.task(own['id'])['status'], 'cancelled')

    def test_foreign_incremental_parent_is_denied_before_model_access(self):
        alice, a_cookie, bob, b_cookie = self.accounts()
        added = self.image_set(bob, b_cookie, 'data/uploads/bob-additions')
        parent = self.store.create_task(alice['projects'][0]['id'], None, 'private-parent', 'sfm',
            'outputs/private-parent', None, {}, status='completed')
        code, _, _ = self.request('/api/start', {'project_id': bob['projects'][0]['id'],
            'method': 'sfm', 'image_set_id': added['id'], 'parent_task_id': parent['id']}, cookie=b_cookie)
        self.assertEqual(code, 403)

    def test_schema_reopen_preserves_accounts_parent_links_and_queue(self):
        alice, a_cookie, _, _ = self.accounts()
        parent = self.queued(alice, 'parent')
        child, _ = self.server.jobs.repository.prepare(project_id=parent['project_id'], image_set_id=None,
            name='child', method='sfm', output='outputs/child', log=None,
            parameters={'parent_task_id': parent['id']}, command=[], request_fingerprint='child')
        reopened = Workspace(self.root)
        self.assertEqual(reopened.task(child['id'])['parent_task_id'], parent['id'])
        self.assertEqual(reopened.task(parent['id'])['status'], 'queued')
        self.assertEqual(reopened.project(parent['project_id'])['owner_id'], alice['user']['id'])

    def test_parent_mutation_while_queued_prevents_algorithm_launch(self):
        alice, a_cookie, _, _ = self.accounts()
        additions = self.image_set(alice, a_cookie, 'data/uploads/additions')
        state = self.store.analyze_images(additions['id'])
        self.store.save_selection(additions['id'], ['0.png', '1.png'], state['report']['fingerprint'])
        folder = self.root / 'outputs/base/sfm'
        photos, model = folder / 'inputs', folder / 'sparse/0'
        photos.mkdir(parents=True)
        model.mkdir(parents=True)
        Image.new('RGB', (20, 16), (70, 70, 70)).save(photos / 'old.png')
        for name in ('cameras.bin', 'images.bin', 'points3D.bin'):
            (model / name).write_bytes(b'fixture-model')
        sqlite3.connect(folder / 'database.db').close()
        for name, document in {
            'config.json': {'images': str(photos)},
            'summary.json': {'images_path': str(photos), 'selected_model': str(model)},
            'status.json': {'status': 'completed'},
            'input_manifest.json': manifest(photos, ['old.png']),
        }.items():
            (folder / name).write_text(json.dumps(document))
        parent = self.store.create_task(alice['projects'][0]['id'], None, 'base', 'sfm',
            'outputs/base', None, {}, status='completed')
        jobs = self.server.jobs
        jobs.scheduler.blocked_reason = 'integration_test_pause'
        result = jobs.start({'project_id': parent['project_id'], 'method': 'sfm',
            'image_set_id': additions['id'], 'parent_task_id': parent['id']}, alice['user']['id'])
        child = self.store.task(result['task_id'])
        self.assertEqual(child['parent_task_id'], parent['id'])
        self.assertIn('--base-sfm', child['command'])
        self.assertEqual(child['parameters']['incremental_parent_fingerprint'], parent_fingerprint(folder))
        (model / 'points3D.bin').write_bytes(b'changed-after-submission')
        claimed = jobs.repository.claim_next(jobs.scheduler.resources)
        with patch('recon_workbench.task_runtime.processes.launch') as launch:
            jobs.scheduler._execute(claimed)
        launch.assert_not_called()
        failed = self.store.task(child['id'])
        self.assertEqual(failed['status'], 'failed')
        self.assertIn('父模型', failed['error'])
