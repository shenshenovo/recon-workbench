"""持久化 FIFO、执行尝试与有条件的状态转换。"""
from contextlib import contextmanager
import json
import sqlite3
import uuid

from .migrations import apply
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

TERMINAL = ('completed', 'failed', 'cancelled', 'interrupted')
ACTIVE = ('starting', 'running', 'cancelling')


class RuntimeConflict(ValueError):
    code = 'state_conflict'


class IdempotencyConflict(RuntimeConflict):
    code = 'idempotency_conflict'


class TaskNotFound(ValueError):
    pass


class TaskRepository:
    def __init__(self, workspace):
        if hasattr(workspace, 'path') and hasattr(workspace, 'connect'):
            self.workspace = workspace
        else:
            from ..workspace import Workspace
            self.workspace = Workspace(workspace)
        self.path = self.workspace.path
        with self.workspace.connect() as db:
            apply(db)

    @contextmanager
    def connect(self, write=False):
        db = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db
            if write:
                db.execute('COMMIT')
        except BaseException:
            if db.in_transaction:
                db.execute('ROLLBACK')
            raise
        finally:
            db.close()

    @staticmethod
    def decode(row):
        if row is None:
            return None
        result = dict(row)
        for field in ('parameters', 'command'):
            if field in result:
                result[field] = json.loads(result[field])
        return result

    def get(self, task_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is None:
            raise TaskNotFound('任务不存在。')
        return self.decode(row)

    def by_key(self, project_id, key, fingerprint):
        if key is None:
            return None
        with self.connect() as db:
            row = db.execute('SELECT * FROM tasks WHERE project_id=? AND idempotency_key=?', (project_id, key)).fetchone()
        if row and row['request_fingerprint'] != fingerprint:
            raise IdempotencyConflict('同一幂等键对应的参数不同。')
        return self.decode(row)

    def prepare(self, *, project_id, image_set_id, name, method, output, log, parameters,
                command, request_fingerprint, idempotency_key=None, task_id=None):
        """先占用幂等键；返回 (任务, 是否本次创建)。"""
        with self.connect(write=True) as db:
            if idempotency_key is not None:
                row = db.execute('SELECT * FROM tasks WHERE project_id=? AND idempotency_key=?', (project_id, idempotency_key)).fetchone()
                if row:
                    if row['request_fingerprint'] != request_fingerprint:
                        raise IdempotencyConflict('同一幂等键对应的参数不同。')
                    return self.decode(row), False
            task_id = task_id or uuid.uuid4().hex
            db.execute('''INSERT INTO tasks (id,project_id,image_set_id,name,method,status,output,log,
                parameters,command,created_at,idempotency_key,request_fingerprint)
                VALUES (?,?,?,?,?,'preparing',?,?,?,?,?,?,?)''',
                (task_id, project_id, image_set_id, name, method, output, log,
                 json.dumps(parameters, ensure_ascii=False), json.dumps(command), now(), idempotency_key, request_fingerprint))
        return self.get(task_id), True

    def publish(self, task_id, parameters, command, input_fingerprint):
        with self.connect(write=True) as db:
            row = db.execute('SELECT status FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row or row['status'] != 'preparing':
                return False
            seq = db.execute('INSERT INTO task_queue_sequence(task_id) VALUES (?)', (task_id,)).lastrowid
            db.execute('''UPDATE tasks SET status='queued',queue_seq=?,parameters=?,command=?,input_fingerprint=?
                WHERE id=? AND status='preparing' ''',
                (seq, json.dumps(parameters, ensure_ascii=False), json.dumps(command), input_fingerprint, task_id))
        return True

    def fail_preparing(self, task_id, error):
        with self.connect(write=True) as db:
            db.execute("UPDATE tasks SET status='failed',error=?,finished_at=? WHERE id=? AND status='preparing'",
                       (str(error), now(), task_id))

    def claim_next(self):
        with self.connect(write=True) as db:
            if db.execute('SELECT 1 FROM task_attempts WHERE cleanup_confirmed=0 LIMIT 1').fetchone():
                return None
            if db.execute("SELECT 1 FROM tasks WHERE status IN ('starting','running','cancelling') LIMIT 1").fetchone():
                return None
            row = db.execute("SELECT * FROM tasks WHERE status='queued' ORDER BY queue_seq LIMIT 1").fetchone()
            if row is None:
                return None
            attempt_id = uuid.uuid4().hex
            number = db.execute('SELECT COALESCE(MAX(attempt_no),0)+1 FROM task_attempts WHERE task_id=?', (row['id'],)).fetchone()[0]
            db.execute('''INSERT INTO task_attempts (id,task_id,attempt_no,status,command,started_at)
                VALUES (?,?,?,'starting',?,?)''', (attempt_id, row['id'], number, row['command'], now()))
            db.execute("UPDATE tasks SET status='starting',current_attempt_id=?,pid=NULL WHERE id=? AND status='queued'", (attempt_id, row['id']))
        task = self.get(row['id'])
        return task

    def set_process(self, task_id, attempt_id, pid, pgid, process_start):
        with self.connect(write=True) as db:
            changed = db.execute("""UPDATE tasks SET status='running',pid=? WHERE id=?
                AND current_attempt_id=? AND status='starting'""", (pid, task_id, attempt_id)).rowcount
            # Even if cancellation won, record the wrapper identity so a
            # crash during cleanup can never turn it into an unknown process.
            db.execute('UPDATE task_attempts SET pid=?,pgid=?,process_start=? WHERE id=? AND cleanup_confirmed=0',
                       (pid, pgid, process_start, attempt_id))
            if changed:
                db.execute("UPDATE task_attempts SET status='running' WHERE id=?", (attempt_id,))
        return bool(changed)

    def request_cancel(self, task_id=None):
        with self.connect(write=True) as db:
            if task_id is None:
                rows = list(db.execute("SELECT id FROM tasks WHERE status IN ('preparing','queued','starting','running','cancelling')"))
                if len(rows) > 1:
                    error = RuntimeConflict('存在多个待处理任务，请提供 task_id。')
                    error.code = 'task_id_required'
                    raise error
                if not rows:
                    return {'cancel_requested': False}
                task_id = rows[0]['id']
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if row is None:
                raise TaskNotFound('任务不存在。')
            status = row['status']
            requested = status in ('preparing', 'queued', *ACTIVE)
            if status in ('preparing', 'queued'):
                status = 'cancelled'
                db.execute("UPDATE tasks SET status=?,cancel_requested_at=?,finished_at=? WHERE id=?", (status, now(), now(), task_id))
            elif status in ('starting', 'running'):
                status = 'cancelling'
                db.execute("UPDATE tasks SET status=?,cancel_requested_at=? WHERE id=?", (status, now(), task_id))
                db.execute("UPDATE task_attempts SET status='cancelling',cancel_requested_at=? WHERE id=?", (now(), row['current_attempt_id']))
        return {'task_id': task_id, 'status': status, 'cancel_requested': requested}

    def finish(self, task_id, attempt_id, status, returncode=None, error=None, cleanup_confirmed=True):
        if status not in TERMINAL:
            raise ValueError('非法终态。')
        with self.connect(write=True) as db:
            row = db.execute('SELECT status,current_attempt_id FROM tasks WHERE id=?', (task_id,)).fetchone()
            if row is None or row['current_attempt_id'] != attempt_id or row['status'] not in ACTIVE:
                return False
            if not cleanup_confirmed:
                status = 'cancelling' if row['status'] == 'cancelling' else 'interrupted'
            elif row['status'] == 'cancelling':
                status = 'cancelled'
            # Artifact registration and terminal publication share this same
            # transaction. File-read/DB failure leaves an observable attempt.
            if status == 'completed':
                self.workspace.register_models(db, task_id)
            db.execute('''UPDATE tasks SET status=?,returncode=?,error=?,finished_at=?,pid=NULL
                WHERE id=? AND current_attempt_id=?''', (status, returncode, error, now(), task_id, attempt_id))
            db.execute('''UPDATE task_attempts SET status=?,returncode=?,error=?,finished_at=?,cleanup_confirmed=?
                WHERE id=?''', (status, returncode, error, now(), int(cleanup_confirmed), attempt_id))
        return True

    def attempts(self, task_id):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM task_attempts WHERE task_id=? ORDER BY attempt_no', (task_id,))]

    def unresolved(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM task_attempts WHERE cleanup_confirmed=0 ORDER BY started_at,id')]

    def reconcile_attempt(self, attempt, cleanup_confirmed, error):
        with self.connect(write=True) as db:
            db.execute("""UPDATE task_attempts SET status='interrupted',cleanup_confirmed=?,error=?,finished_at=?
                WHERE id=? AND cleanup_confirmed=0""", (int(cleanup_confirmed), error, now(), attempt['id']))
            db.execute("""UPDATE tasks SET status='interrupted',pid=NULL,error=?,finished_at=?
                WHERE id=? AND current_attempt_id=? AND status IN ('starting','running','cancelling','interrupted')""",
                (error, now(), attempt['task_id'], attempt['id']))

    def queue(self, project_id=None, blocked_reason=None):
        if project_id:
            self.workspace.project(project_id)
        with self.connect() as db:
            queued = [self.decode(row) for row in db.execute("SELECT * FROM tasks WHERE status='queued' ORDER BY queue_seq")]
            running = [self.decode(row) for row in db.execute("SELECT * FROM tasks WHERE status IN ('starting','running','cancelling') ORDER BY created_at,id")]
        for index, row in enumerate(queued, 1):
            row.update(task_id=row['id'], queue_position=index,
                       waiting_reason=blocked_reason or ('worker_busy' if running else None))
        for row in running:
            row.update(task_id=row['id'], queue_position=None, waiting_reason=None)
        return {'dispatch_paused': bool(blocked_reason), 'blocked_reason': blocked_reason,
                'running': [row for row in running if not project_id or row['project_id'] == project_id],
                'queued': [row for row in queued if not project_id or row['project_id'] == project_id]}

    def response(self, task_id, blocked_reason=None):
        task = self.get(task_id)
        queued = next((row for row in self.queue(blocked_reason=blocked_reason)['queued'] if row['id'] == task_id), {})
        return {'task_id': task_id, 'status': task['status'], 'output': task['output'], 'pid': task['pid'],
                'queue_position': queued.get('queue_position'), 'waiting_reason': queued.get('waiting_reason')}
