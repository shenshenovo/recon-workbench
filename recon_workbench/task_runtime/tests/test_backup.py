"""离线备份与新目录恢复的完整性测试。"""
import json
import io
import errno
import fcntl
import hashlib
import os
import subprocess
import sys
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from recon_workbench.task_runtime.backup import BackupError, create_backup, restore_backup, verify_archive
from recon_workbench.workspace import Workspace
from recon_workbench.task_runtime.repository import TaskRepository
from recon_workbench.task_runtime.scheduler import Scheduler
from recon_workbench.task_runtime.snapshots import digest
from recon_workbench.task_runtime.validators import validate_input_snapshot


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'source'
        self.store = Workspace(self.root)
        self.photo = self.root / 'data/photos/a.jpg'
        self.photo.parent.mkdir(parents=True)
        self.photo.write_bytes(b'photo')
        self.image_set = self.store.register_set('default', self.root / 'data/photos')
        self.output = self.root / 'outputs/scene'
        self.output.mkdir(parents=True)
        (self.output / 'model.ply').write_bytes(b'ply')
        (self.output / 'summary.json').write_text(json.dumps({'method': 'sfm'}))
        (self.output / 'status.json').write_text(json.dumps({'status': 'completed'}))
        task = self.store.create_task('default', self.image_set['id'], 'scene', 'sfm', self.output,
                                      self.root / 'outputs/.ui_logs/scene.log', {'input': 'data/photos'},
                                      status='completed')
        with self.store.connect() as db:
            db.execute('INSERT INTO models VALUES (?,?,?,?,?)',
                       ('model-id', task['id'], 'ply', 'outputs/scene/model.ply', 3))
        self.task = task

    def tearDown(self):
        self.temp.cleanup()

    def test_backup_verify_restore_rewrites_paths_and_keeps_queue(self):
        archive = Path(self.temp.name) / 'scene.tar.gz'
        result = create_backup(self.root, archive)
        self.assertGreater(result['files'], 3)
        manifest = verify_archive(archive)
        self.assertEqual(manifest['format_version'], 1)
        target = Path(self.temp.name) / 'restored'
        restored = restore_backup(archive, target)
        self.assertTrue((target / 'data/photos/a.jpg').is_file())
        self.assertEqual((target / 'outputs/scene/model.ply').read_bytes(), b'ply')
        self.assertTrue(restored['dispatch_paused'])
        copied = Workspace(target)
        row = copied.task(self.task['id'])
        self.assertEqual(row['output'], 'outputs/scene')
        self.assertEqual(copied.models(self.task['id'])[0]['path'], 'outputs/scene/model.ply')
        self.assertTrue(copied.path.is_file())

    def test_offline_cli_roundtrip_and_failure_exit_code(self):
        prefix = [sys.executable, '-m', 'recon_workbench.task_runtime.backup']
        archive = Path(self.temp.name) / 'cli.tar.gz'
        target = Path(self.temp.name) / 'cli-restored'
        commands = [
            ['create', '--workspace', str(self.root), '--archive', str(archive)],
            ['verify', '--archive', str(archive)],
            ['restore', '--archive', str(archive), '--target', str(target)],
        ]
        for command in commands:
            result = subprocess.run(prefix + command, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
        failure = subprocess.run(prefix + commands[-1], capture_output=True, text=True, timeout=15)
        self.assertEqual(failure.returncode, 1)
        self.assertIn('新目录', failure.stderr)
        self.assertEqual((target / 'data/photos/a.jpg').read_bytes(), b'photo')

    def test_missing_registered_photo_or_model_rejects_backup(self):
        self.photo.unlink()
        with self.assertRaises(BackupError):
            create_backup(self.root, Path(self.temp.name) / 'missing-photo.tar.gz')
        self.photo.write_bytes(b'photo')
        (self.output / 'model.ply').unlink()
        with self.assertRaises(BackupError):
            create_backup(self.root, Path(self.temp.name) / 'missing-model.tar.gz')

    def test_active_task_rejects_backup_and_archive_tamper_is_detected(self):
        with self.store.connect() as db:
            db.execute("UPDATE tasks SET status='running' WHERE id=?", (self.task['id'],))
        with self.assertRaises(BackupError):
            create_backup(self.root, Path(self.temp.name) / 'active.tar.gz')
        with self.store.connect() as db:
            db.execute("UPDATE tasks SET status='completed' WHERE id=?", (self.task['id'],))
        archive = Path(self.temp.name) / 'tamper.tar.gz'
        create_backup(self.root, archive)
        altered = Path(self.temp.name) / 'altered.tar.gz'
        with tarfile.open(archive, 'r:gz') as source, tarfile.open(altered, 'w:gz') as target:
            for member in source:
                content = source.extractfile(member).read() if member.isfile() else None
                if member.name == 'data/photos/a.jpg':
                    content = b'tampered'
                if content is None:
                    target.addfile(member)
                else:
                    member.size = len(content)
                    import io
                    target.addfile(member, io.BytesIO(content))
        with self.assertRaises(BackupError):
            verify_archive(altered)

    def rewrite_archive(self, archive, name, transform):
        target = Path(self.temp.name) / name
        with tarfile.open(archive, 'r:gz') as original:
            members = [(item, original.extractfile(item).read()) for item in original]
        with tarfile.open(target, 'w:gz') as output:
            transform(members)
            for item, content in members:
                item.size = len(content)
                output.addfile(item, io.BytesIO(content))
        return target

    def test_rejects_traversal_links_duplicates_and_future_format(self):
        archive = Path(self.temp.name) / 'valid.tar.gz'
        create_backup(self.root, archive)
        for index, kind in enumerate(('traversal', 'symlink', 'hardlink', 'duplicate', 'version')):
            def transform(members):
                if kind == 'traversal':
                    members[0][0].name = 'data/../../outside'
                elif kind in ('symlink', 'hardlink'):
                    members[0][0].type = tarfile.SYMTYPE if kind == 'symlink' else tarfile.LNKTYPE
                    members[0][0].linkname = '/outside'
                elif kind == 'duplicate':
                    members.append(members[0])
                else:
                    item, content = members[-1]
                    manifest = json.loads(content)
                    manifest['format_version'] = 999
                    members[-1] = (item, json.dumps(manifest).encode())
            with self.subTest(kind=kind):
                bad = self.rewrite_archive(archive, f'bad-{index}.tar.gz', transform)
                destination = Path(self.temp.name) / f'target-{index}'
                with self.assertRaises(BackupError):
                    restore_backup(bad, destination)
                self.assertFalse(destination.exists())
                self.assertEqual(self.photo.read_bytes(), b'photo')

    def test_truncation_size_limits_and_existing_target(self):
        archive = Path(self.temp.name) / 'valid.tar.gz'
        create_backup(self.root, archive)
        with self.assertRaises(BackupError):
            verify_archive(archive, max_unpacked_bytes=1)
        truncated = Path(self.temp.name) / 'truncated.tar.gz'
        truncated.write_bytes(archive.read_bytes()[:100])
        with self.assertRaises(BackupError):
            verify_archive(truncated)
        with self.assertRaises(BackupError):
            restore_backup(archive, self.root)
        with self.assertRaises(BackupError):
            create_backup(self.root, archive)

    def test_future_database_version_and_omitted_assets_are_rejected(self):
        archive = Path(self.temp.name) / 'valid.tar.gz'
        create_backup(self.root, archive)
        def omit_model(members):
            members[:] = [(item, content) for item, content in members if item.name != 'outputs/scene/model.ply']
            item, content = members[-1]
            manifest = json.loads(content)
            manifest['files'] = [entry for entry in manifest['files'] if entry['path'] != 'outputs/scene/model.ply']
            manifest['file_count'] = len(manifest['files'])
            manifest['unpacked_bytes'] = sum(entry['bytes'] for entry in manifest['files'])
            members[-1] = (item, json.dumps(manifest).encode())
        incomplete = self.rewrite_archive(archive, 'omitted.tar.gz', omit_model)
        with self.assertRaises(BackupError):
            verify_archive(incomplete)
        with self.store.connect() as db:
            db.execute("UPDATE task_runtime_meta SET value='999' WHERE key='schema_version'")
        with self.assertRaises(BackupError):
            create_backup(self.root, Path(self.temp.name) / 'future-database.tar.gz')

    def test_missing_or_changed_snapshot_files_reject_backup(self):
        repo = TaskRepository(self.store)
        snapshot = self.root / 'data/snapshot'
        snapshot.mkdir()
        (snapshot / 'a.jpg').write_bytes(b'original')
        (snapshot / 'b.jpg').write_bytes(b'second')
        identity = {'files': [{'name': name, 'sha256': hashlib.sha256((snapshot / name).read_bytes()).hexdigest()}
                              for name in ('a.jpg', 'b.jpg')]}
        fingerprint = digest(identity)
        (snapshot / 'input.json').write_text(json.dumps({'identity': identity, 'fingerprint': fingerprint}))
        params = {'input': 'data/snapshot', 'input_manifest': 'data/snapshot/input.json'}
        task = repo.prepare(project_id='default', image_set_id=None, name='pending', method='sfm',
                            parameters=params, output='outputs/pending', log=None, command=[],
                            request_fingerprint='request')[0]
        repo.publish(task['id'], params, ['true'], fingerprint)
        (snapshot / 'a.jpg').unlink()
        with self.assertRaises(BackupError):
            create_backup(self.root, Path(self.temp.name) / 'missing-snapshot.tar.gz')
        (snapshot / 'a.jpg').write_bytes(b'changed')
        with self.assertRaises(BackupError):
            create_backup(self.root, Path(self.temp.name) / 'changed-snapshot.tar.gz')

    def test_workspace_lock_symlink_rejection_and_hardlink_copy(self):
        with (self.root / 'data/workbench.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BackupError):
                create_backup(self.root, Path(self.temp.name) / 'locked.tar.gz')
        link = self.photo.with_name('link.jpg')
        link.symlink_to(self.photo)
        with self.assertRaises(BackupError):
            create_backup(self.root, Path(self.temp.name) / 'symlink.tar.gz')
        link.unlink()
        os.link(self.photo, link)
        archive = Path(self.temp.name) / 'hardlink.tar.gz'
        create_backup(self.root, archive)
        with tarfile.open(archive, 'r:gz') as tar:
            self.assertTrue(all(member.isfile() and not member.islnk() for member in tar))

    def test_disk_failure_does_not_publish_or_overwrite(self):
        archive = Path(self.temp.name) / 'valid.tar.gz'
        create_backup(self.root, archive)
        destination = Path(self.temp.name) / 'disk-failure'
        with patch('recon_workbench.task_runtime.backup._rewrite_database',
                   side_effect=OSError(errno.ENOSPC, 'No space left on device')):
            with self.assertRaises(OSError):
                restore_backup(archive, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(list(destination.parent.glob('.disk-failure.*.restore')), [])
        self.assertEqual(self.photo.read_bytes(), b'photo')

    def test_restore_preserves_audit_commands_and_requires_explicit_requeue(self):
        repo = TaskRepository(self.store)
        snapshot = self.root / 'data/snapshot'
        snapshot.mkdir()
        data = {'source_sfm': str(self.output), 'status': 'prepared'}
        (snapshot / 'dataset.json').write_text(json.dumps(data))
        content = (snapshot / 'dataset.json').read_bytes()
        identity = {'files': [{'name': 'dataset.json', 'bytes': len(content),
                               'sha256': hashlib.sha256(content).hexdigest()}]}
        fingerprint = digest(identity)
        (snapshot / 'input.json').write_text(json.dumps({'identity': identity, 'fingerprint': fingerprint}))
        params = {'input': 'data/snapshot', 'input_manifest': 'data/snapshot/input.json'}
        command = ['/old/python', str(self.root / 'main.py'), '--output', str(self.root / 'outputs/pending')]
        task = repo.prepare(project_id='default', image_set_id=None, name='pending', method='sfm',
                            parameters=params, output='outputs/pending', log='outputs/.ui_logs/pending.log',
                            command=command, request_fingerprint='request')[0]
        repo.publish(task['id'], params, command, fingerprint)
        archive = Path(self.temp.name) / 'queued.tar.gz'
        create_backup(self.root, archive)
        target = Path(self.temp.name) / 'relocated'
        restore_backup(archive, target)
        copied = Workspace(target)
        row = TaskRepository(copied).get(task['id'])
        self.assertEqual(row['status'], 'interrupted')
        self.assertEqual(row['command'], command)
        self.assertTrue(validate_input_snapshot(copied, row)['valid'])
        restored_json = json.loads((target / 'data/snapshot/dataset.json').read_text())
        self.assertEqual(restored_json['source_sfm'], str(target / 'outputs/scene'))
        scheduler = Scheduler(copied)
        self.assertEqual(scheduler.blocked_reason, 'restored_workspace_confirmation')
        self.assertFalse(scheduler.resume_dispatch()['dispatch_paused'])
        scheduler.close()


if __name__ == '__main__':
    unittest.main()
