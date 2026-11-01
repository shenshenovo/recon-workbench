import sqlite3
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from recon_workbench.task_runtime import migrations
from recon_workbench.task_runtime.repository import IdempotencyConflict, TaskRepository
from recon_workbench.task_runtime.scheduler import Scheduler
from recon_workbench.workspace import Workspace


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = Workspace(self.root)
        self.repo = TaskRepository(self.workspace)

    def tearDown(self):
        self.temp.cleanup()

    def task(self, name, command, *, key=None, fingerprint='fp'):
        task = self.workspace.create_task('default', None, name, 'fixture',
                                           self.root / 'outputs' / name, self.root / 'logs' / (name + '.log'), {}, command=command,
                                           status='preparing')
        self.assertTrue(self.repo.publish(task['id'], {}, command, fingerprint))
        if key:
            with self.repo.connect(write=True) as db:
                db.execute('UPDATE tasks SET idempotency_key=?,request_fingerprint=? WHERE id=?', (key, fingerprint, task['id']))
        return self.repo.get(task['id'])

    def test_claim_is_single_winner_and_fifo(self):
        a = self.task('a', [sys.executable, '-c', 'pass'])
        b = self.task('b', [sys.executable, '-c', 'pass'])
        values = []
        threads = [threading.Thread(target=lambda: values.append(self.repo.claim_next())) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(sum(item is not None for item in values), 1)
        self.assertEqual(values[0]['id'], a['id'])

    def test_scheduler_runs_second_after_first(self):
        marker = self.root / 'marker'
        code = "from pathlib import Path; import sys,time,json; o=Path(sys.argv[3]); o.mkdir(parents=True); (o/'status.json').write_text(json.dumps({'status':'completed'})); (o/'summary.json').write_text(json.dumps({'method':'fixture'})); Path(sys.argv[1]).open('a').write(sys.argv[2]+'\\n'); time.sleep(.1)"
        a = self.task('a', [sys.executable, '-c', code, str(marker), 'a', str(self.root/'outputs/a')])
        b = self.task('b', [sys.executable, '-c', code, str(marker), 'b', str(self.root/'outputs/b')])
        scheduler = Scheduler(self.workspace, popen=subprocess.Popen)
        scheduler.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.repo.get(b['id'])['status'] != 'completed':
            time.sleep(.02)
        scheduler.close()
        self.assertEqual(marker.read_text().splitlines(), ['a', 'b'])

    def test_cancel_keeps_terminal_state_after_process_tree_cleanup(self):
        code = "import signal,subprocess,time; signal.signal(signal.SIGINT,signal.SIG_IGN); subprocess.Popen(['sleep','30']); time.sleep(30)"
        task = self.task('cancel', [sys.executable, '-c', code])
        scheduler = Scheduler(self.workspace, popen=subprocess.Popen, cancel_timeouts=(.1, .1, .1))
        scheduler.start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and self.repo.get(task['id'])['status'] != 'running':
            time.sleep(.01)
        result = scheduler.cancel(task['id'])
        self.assertEqual(result['status'], 'cancelling')
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.repo.get(task['id'])['status'] == 'cancelling':
            time.sleep(.02)
        scheduler.close()
        self.assertEqual(self.repo.get(task['id'])['status'], 'cancelled')

    def test_migration_failure_rolls_back(self):
        db = sqlite3.connect(self.workspace.path)
        real = migrations._add_column
        calls = []
        def fail(connection, table, name, definition):
            real(connection, table, name, definition)
            calls.append(name)
            if len(calls) == 2:
                raise OSError('simulated disk failure')
        # Existing Workspace has already migrated; use a copied legacy DB.
        legacy = sqlite3.connect(self.root / 'legacy.sqlite3')
        legacy.executescript('CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT,created_at TEXT); CREATE TABLE tasks(id TEXT PRIMARY KEY,project_id TEXT,name TEXT,method TEXT,status TEXT,output TEXT,parameters TEXT,command TEXT,created_at TEXT);')
        legacy.commit()
        with patch.object(migrations, '_add_column', side_effect=fail), self.assertRaises(OSError):
            migrations.apply(legacy)
        self.assertEqual(legacy.execute("SELECT count(*) FROM sqlite_master WHERE name='task_runtime_meta'").fetchone()[0], 0)
        legacy.close(); db.close()

    def test_legacy_rows_survive_versioned_upgrade_and_repeat(self):
        legacy = sqlite3.connect(self.root / 'data/old.sqlite3')
        legacy.executescript('''
            CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE tasks(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,image_set_id TEXT,name TEXT NOT NULL,
                method TEXT NOT NULL,status TEXT NOT NULL,output TEXT NOT NULL UNIQUE,log TEXT,
                parameters TEXT NOT NULL,command TEXT NOT NULL,pid INTEGER,returncode INTEGER,error TEXT,
                stage TEXT,elapsed REAL,created_at TEXT NOT NULL,finished_at TEXT);
            CREATE TABLE models(id TEXT PRIMARY KEY,task_id TEXT NOT NULL,kind TEXT NOT NULL,path TEXT NOT NULL,bytes INTEGER);
            INSERT INTO projects VALUES('default','legacy','2020-01-01');
            INSERT INTO tasks(id,project_id,name,method,status,output,parameters,command,created_at)
                VALUES('old-task','default','old','sfm','completed','outputs/old','{}','[]','2020-01-01');
            INSERT INTO models VALUES('old-model','old-task','ply','outputs/old/model.ply',12);
        ''')
        legacy.commit()
        self.assertEqual(migrations.apply(legacy), 1)
        self.assertEqual(migrations.apply(legacy), 1)
        self.assertEqual(legacy.execute('SELECT id FROM tasks').fetchall(), [('old-task',)])
        self.assertEqual(legacy.execute('SELECT id FROM models').fetchall(), [('old-model',)])
        legacy.close()

    def test_idempotency_conflict_is_explicit(self):
        a = self.task('a', [], key='same', fingerprint='one')
        with self.assertRaises(IdempotencyConflict):
            self.repo.by_key('default', 'same', 'two')
        self.assertEqual(self.repo.get(a['id'])['status'], 'queued')


if __name__ == '__main__':
    unittest.main()
