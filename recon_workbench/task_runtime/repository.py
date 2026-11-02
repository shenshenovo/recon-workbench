"""持久化 FIFO、执行尝试、资源分配与有条件的状态转换。"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import hashlib
import sqlite3
import uuid

from .migrations import apply
from .validators import execution_fingerprint


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


TERMINAL = ('completed', 'failed', 'cancelled', 'interrupted')
ACTIVE = ('starting', 'running', 'cancelling')
DEFAULT_RESOURCE_REQUEST = {'cpu_slots': 1, 'gpu_required': False, 'gpu_backend': None}


def parameters_fingerprint(parameters):
    encoded = json.dumps(parameters or {}, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


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
        for field in ('parameters', 'command', 'resource_request'):
            if field in result and result[field] is not None:
                result[field] = json.loads(result[field])
        return result

    @staticmethod
    def _allocation(db, attempt_id):
        if not attempt_id:
            return None
        row = db.execute("""
            SELECT attempt_id,task_id,cpu_slots,gpu_id,state,allocated_at,released_at
            FROM resource_allocations
            WHERE attempt_id=? AND state='allocated'
        """, (attempt_id,)).fetchone()
        return dict(row) if row else None

    @classmethod
    def _with_allocation(cls, db, row):
        task = cls.decode(row)
        if task is not None:
            task['resource_request'] = task.get('resource_request') or dict(DEFAULT_RESOURCE_REQUEST)
            task['allocation'] = cls._allocation(db, task.get('current_attempt_id'))
        return task

    def get(self, task_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            result = self._with_allocation(db, row)
        if row is None:
            raise TaskNotFound('任务不存在。')
        return result

    def by_key(self, project_id, key, fingerprint):
        if key is None:
            return None
        with self.connect() as db:
            row = db.execute('SELECT * FROM tasks WHERE project_id=? AND idempotency_key=?',
                             (project_id, key)).fetchone()
            result = self._with_allocation(db, row)
        if row and row['request_fingerprint'] != fingerprint:
            raise IdempotencyConflict('同一幂等键对应的参数不同。')
        return result

    def prepare(self, *, project_id, image_set_id, name, method, output, log, parameters,
                command, request_fingerprint, idempotency_key=None, task_id=None,
                resource_request=None):
        """先占用幂等键；返回 (任务, 是否本次创建)。"""
        request = resource_request or DEFAULT_RESOURCE_REQUEST
        with self.connect(write=True) as db:
            if idempotency_key is not None:
                row = db.execute('SELECT * FROM tasks WHERE project_id=? AND idempotency_key=?',
                                 (project_id, idempotency_key)).fetchone()
                if row:
                    if row['request_fingerprint'] != request_fingerprint:
                        raise IdempotencyConflict('同一幂等键对应的参数不同。')
                    return self._with_allocation(db, row), False
            task_id = task_id or uuid.uuid4().hex
            db.execute('''INSERT INTO tasks (id,project_id,image_set_id,name,method,status,output,output_root,log,
                parameters,command,created_at,idempotency_key,request_fingerprint,resource_request)
                VALUES (?,?,?,?,?,'preparing',?,?,?,?,?,?,?,?,?)''',
                (task_id, project_id, image_set_id, name, method, output, output, log,
                 json.dumps(parameters, ensure_ascii=False), json.dumps(command), now(), idempotency_key,
                 request_fingerprint, json.dumps(request, ensure_ascii=False)))
        return self.get(task_id), True

    def publish(self, task_id, parameters, command, input_fingerprint, resource_request=None):
        with self.connect(write=True) as db:
            row = db.execute('SELECT status FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row or row['status'] != 'preparing':
                return False
            seq = db.execute('INSERT INTO task_queue_sequence(task_id) VALUES (?)', (task_id,)).lastrowid
            db.execute('''UPDATE tasks SET status='queued',queue_seq=?,parameters=?,command=?,input_fingerprint=?,
                resource_request=COALESCE(?, resource_request)
                WHERE id=? AND status='preparing' ''',
                (seq, json.dumps(parameters, ensure_ascii=False), json.dumps(command), input_fingerprint,
                 json.dumps(resource_request, ensure_ascii=False) if resource_request else None, task_id))
        return True

    def fail_preparing(self, task_id, error):
        with self.connect(write=True) as db:
            db.execute("UPDATE tasks SET status='failed',error=?,finished_at=? WHERE id=? AND status='preparing'",
                       (str(error), now(), task_id))

    def claim_next(self, resources=None):
        if resources is not None:
            resources.prepare_claim()
        with self.connect(write=True) as db:
            # A normal active attempt has cleanup_confirmed=0 until it exits;
            # only an attempt left in a non-active state blocks new dispatch.
            if db.execute("""
                SELECT 1 FROM task_attempts a JOIN tasks t ON t.id=a.task_id
                WHERE a.cleanup_confirmed=0 AND a.status!='queued'
                  AND t.status NOT IN ('starting','running','cancelling')
                LIMIT 1
            """).fetchone():
                return None
            if resources is None and db.execute(
                    "SELECT 1 FROM tasks WHERE status IN ('starting','running','cancelling') LIMIT 1").fetchone():
                return None
            rows = list(db.execute("SELECT * FROM tasks WHERE status='queued' ORDER BY queue_seq"))
            row = None
            allocation = None
            for candidate in rows:
                if resources is None:
                    row = candidate
                    allocation = {'cpu_slots': 1, 'gpu_id': None}
                    break
                allocation, _ = resources.allocation_candidate(db, candidate['resource_request'])
                if allocation is not None:
                    row = candidate
                    break
            if row is None:
                return None
            existing_attempt = None
            if row['current_attempt_id']:
                existing_attempt = db.execute('''SELECT * FROM task_attempts
                    WHERE id=? AND task_id=? AND status='queued' AND cleanup_confirmed=0''',
                    (row['current_attempt_id'], row['id'])).fetchone()
            if existing_attempt:
                attempt_id = existing_attempt['id']
                db.execute("UPDATE task_attempts SET status='starting',started_at=?,command=? WHERE id=?",
                           (now(), row['command'], attempt_id))
            else:
                attempt_id = uuid.uuid4().hex
                number = db.execute('SELECT COALESCE(MAX(attempt_no),0)+1 FROM task_attempts WHERE task_id=?',
                                    (row['id'],)).fetchone()[0]
                output_path = row['output_root'] or row['output']
                runtime_fingerprint = execution_fingerprint(
                    self.workspace.root, row['method'], json.loads(row['parameters']))
                db.execute('''INSERT INTO task_attempts (id,task_id,attempt_no,status,command,started_at,
                    output_path,log_path,request_fingerprint,input_fingerprint,execution_fingerprint,
                    parameters_fingerprint)
                    VALUES (?,?,?,'starting',?,?,?,?,?,?,?,?)''',
                    (attempt_id, row['id'], number, row['command'], now(), output_path,
                     row['log'], row['request_fingerprint'], row['input_fingerprint'], runtime_fingerprint,
                     parameters_fingerprint(json.loads(row['parameters'] or '{}'))))
            if resources is not None:
                db.execute('''INSERT INTO resource_allocations
                    (attempt_id,task_id,cpu_slots,gpu_id,state,allocated_at)
                    VALUES (?,?,?,?,'allocated',?)''',
                    (attempt_id, row['id'], allocation['cpu_slots'], allocation['gpu_id'], now()))
            db.execute("UPDATE tasks SET status='starting',current_attempt_id=?,pid=NULL WHERE id=? AND status='queued'",
                       (attempt_id, row['id']))
        claimed = self.get(row['id'])
        attempt = self.attempt(attempt_id)
        claimed['attempt_output'] = attempt.get('output_path') or claimed['output']
        claimed['attempt_log'] = attempt.get('log_path') or claimed.get('log')
        return claimed

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
                db.execute("UPDATE tasks SET status=?,cancel_requested_at=?,finished_at=? WHERE id=?",
                           (status, now(), now(), task_id))
                if row['current_attempt_id']:
                    db.execute("""UPDATE task_attempts SET status='cancelled',cleanup_confirmed=1,
                        cancel_requested_at=?,finished_at=? WHERE id=? AND status='queued'""",
                        (now(), now(), row['current_attempt_id']))
            elif status in ('starting', 'running'):
                status = 'cancelling'
                db.execute("UPDATE tasks SET status=?,cancel_requested_at=? WHERE id=?", (status, now(), task_id))
                db.execute("UPDATE task_attempts SET status='cancelling',cancel_requested_at=? WHERE id=?",
                           (now(), row['current_attempt_id']))
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
            attempt = db.execute('SELECT output_path FROM task_attempts WHERE id=?', (attempt_id,)).fetchone()
            published_output = attempt['output_path'] if attempt and attempt['output_path'] else None
            # A successful retry publishes its isolated output only after the
            # result is complete. Failed attempts never move the task's result pointer.
            if status == 'completed' and published_output:
                db.execute('UPDATE tasks SET output=? WHERE id=? AND current_attempt_id=?',
                           (published_output, task_id, attempt_id))
            # Artifact registration and terminal publication share this same
            # transaction. File-read/DB failure leaves an observable attempt.
            if status == 'completed':
                self.workspace.register_models(db, task_id)
            db.execute('''UPDATE tasks SET status=?,returncode=?,error=?,finished_at=?,pid=NULL
                WHERE id=? AND current_attempt_id=?''', (status, returncode, error, now(), task_id, attempt_id))
            db.execute('''UPDATE task_attempts SET status=?,returncode=?,error=?,finished_at=?,cleanup_confirmed=?
                WHERE id=?''', (status, returncode, error, now(), int(cleanup_confirmed), attempt_id))
            if cleanup_confirmed:
                db.execute("""UPDATE resource_allocations SET state='released',released_at=?
                    WHERE attempt_id=? AND state='allocated'""", (now(), attempt_id))
        return True

    def attempts(self, task_id):
        with self.connect() as db:
            rows = []
            for row in db.execute('''SELECT a.*,r.cpu_slots,r.gpu_id,r.state AS allocation_state,
                    r.allocated_at,r.released_at
                    FROM task_attempts a LEFT JOIN resource_allocations r ON r.attempt_id=a.id
                    WHERE a.task_id=? ORDER BY a.attempt_no''', (task_id,)):
                item = dict(row)
                item['command'] = json.loads(item['command'])
                if item.get('recovery_plan'):
                    item['recovery_plan'] = json.loads(item['recovery_plan'])
                if item.get('allocation_state') is not None:
                    item['allocation'] = {
                        'cpu_slots': item.pop('cpu_slots'), 'gpu_id': item.pop('gpu_id'),
                        'state': item.pop('allocation_state'), 'allocated_at': item.pop('allocated_at'),
                        'released_at': item.pop('released_at'),
                    }
                else:
                    for key in ('cpu_slots', 'gpu_id', 'allocation_state', 'allocated_at', 'released_at'):
                        item.pop(key, None)
                    item['allocation'] = None
                rows.append(item)
            return rows

    def unresolved(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM task_attempts WHERE cleanup_confirmed=0 "
                "AND status IN ('starting','running','cancelling','interrupted') ORDER BY started_at,id")]

    def attempt(self, attempt_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM task_attempts WHERE id=?', (attempt_id,)).fetchone()
        if row is None:
            raise TaskNotFound('执行尝试不存在。')
        result = dict(row)
        result['command'] = json.loads(result['command'])
        if result.get('recovery_plan'):
            result['recovery_plan'] = json.loads(result['recovery_plan'])
        return result

    def enqueue_recovery(self, task_id, plan, command, output_path, log_path, attempt_id=None):
        """Atomically queue one recovery request, reserving its attempt identity."""
        source_id = plan['source_attempt_id']
        fingerprint = plan['plan_fingerprint']
        with self.connect(write=True) as db:
            task = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if task is None:
                raise TaskNotFound('任务不存在。')
            existing = db.execute('''SELECT id,status FROM task_attempts
                WHERE task_id=? AND resume_fingerprint=? ORDER BY attempt_no DESC LIMIT 1''',
                (task_id, fingerprint)).fetchone()
            if existing and existing['status'] in ('queued', 'starting', 'running', 'cancelling'):
                return {'attempt_id': existing['id'], 'created': False}
            if task['status'] not in ('failed', 'interrupted'):
                error = RuntimeConflict('仅失败或中断的任务可以恢复。')
                error.code = 'task_not_resumable'
                raise error
            if task['current_attempt_id'] != source_id:
                error = RuntimeConflict('任务在恢复预检后发生变化，请重新检查。')
                error.code = 'resume_plan_changed'
                raise error
            if (parameters_fingerprint(json.loads(task['parameters'] or '{}')) != plan.get('parameters_fingerprint') or
                    task['input_fingerprint'] != plan.get('input_fingerprint') or
                    task['request_fingerprint'] != plan.get('request_fingerprint')):
                error = RuntimeConflict('输入或参数在恢复预检后发生变化，请重新检查。')
                error.code = 'resume_plan_changed'
                raise error
            attempt_id = attempt_id or uuid.uuid4().hex
            number = db.execute('SELECT COALESCE(MAX(attempt_no),0)+1 FROM task_attempts WHERE task_id=?',
                                (task_id,)).fetchone()[0]
            db.execute('DELETE FROM task_queue_sequence WHERE task_id=?', (task_id,))
            seq = db.execute('INSERT INTO task_queue_sequence(task_id) VALUES (?)', (task_id,)).lastrowid
            db.execute('''INSERT INTO task_attempts
                (id,task_id,attempt_no,status,command,started_at,cleanup_confirmed,output_path,log_path,
                 source_attempt_id,resume_fingerprint,recovery_plan,request_fingerprint,input_fingerprint,
                 execution_fingerprint,parameters_fingerprint)
                VALUES (?,?,?,'queued',?,?,0,?,?,?,?,?,?,?,?,?)''',
                (attempt_id, task_id, number, json.dumps(command), now(), output_path, log_path, source_id,
                 fingerprint, json.dumps(plan, ensure_ascii=False), task['request_fingerprint'],
                 task['input_fingerprint'], plan.get('execution_fingerprint'),
                 parameters_fingerprint(json.loads(task['parameters'] or '{}'))))
            db.execute('''UPDATE tasks SET status='queued',queue_seq=?,current_attempt_id=?,command=?,log=?,pid=NULL,
                error=NULL,returncode=NULL,cancel_requested_at=NULL,finished_at=NULL WHERE id=? AND status IN ('failed','interrupted')''',
                (seq, attempt_id, json.dumps(command), log_path, task_id))
        return {'attempt_id': attempt_id, 'created': True}

    def persistent_dispatch_pause(self):
        with self.connect() as db:
            row = db.execute("SELECT value FROM task_runtime_meta WHERE key='dispatch_paused'").fetchone()
        return bool(row and row['value'] == '1')

    def set_persistent_dispatch_pause(self, paused):
        with self.connect(write=True) as db:
            db.execute("INSERT INTO task_runtime_meta(key,value) VALUES ('dispatch_paused',?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", ('1' if paused else '0',))

    def reconcile_attempt(self, attempt, cleanup_confirmed, error):
        with self.connect(write=True) as db:
            db.execute("""UPDATE task_attempts SET status='interrupted',cleanup_confirmed=?,error=?,finished_at=?
                WHERE id=? AND cleanup_confirmed=0""", (int(cleanup_confirmed), error, now(), attempt['id']))
            db.execute("""UPDATE tasks SET status='interrupted',pid=NULL,error=?,finished_at=?
                WHERE id=? AND current_attempt_id=? AND status IN ('starting','running','cancelling','interrupted')""",
                       (error, now(), attempt['task_id'], attempt['id']))
            if cleanup_confirmed:
                db.execute("""UPDATE resource_allocations SET state='released',released_at=?
                    WHERE attempt_id=? AND state='allocated'""", (now(), attempt['id']))

    def queue(self, project_id=None, blocked_reason=None, resources=None):
        if project_id:
            self.workspace.project(project_id)
        if resources is not None:
            resources.prepare_claim()
        with self.connect() as db:
            queued = [self._with_allocation(db, row) for row in db.execute(
                "SELECT * FROM tasks WHERE status='queued' ORDER BY queue_seq")]
            running = [self._with_allocation(db, row) for row in db.execute(
                "SELECT * FROM tasks WHERE status IN ('starting','running','cancelling') ORDER BY created_at,id")]
            for index, row in enumerate(queued, 1):
                reason = blocked_reason
                if reason is None and resources is not None:
                    _, reason = resources.allocation_candidate(db, row.get('resource_request'))
                if reason is None and resources is None:
                    reason = 'worker_busy' if running else None
                row.update(task_id=row['id'], queue_position=index, waiting_reason=reason)
            for row in running:
                row.update(task_id=row['id'], queue_position=None, waiting_reason=None)
        return {'dispatch_paused': bool(blocked_reason), 'blocked_reason': blocked_reason,
                'running': [row for row in running if not project_id or row['project_id'] == project_id],
                'queued': [row for row in queued if not project_id or row['project_id'] == project_id]}

    def response(self, task_id, blocked_reason=None, resources=None):
        task = self.get(task_id)
        queued = next((row for row in self.queue(blocked_reason=blocked_reason, resources=resources)['queued']
                       if row['id'] == task_id), {})
        return {'task_id': task_id, 'status': task['status'], 'output': task['output'], 'pid': task['pid'],
                'queue_position': queued.get('queue_position'), 'waiting_reason': queued.get('waiting_reason'),
                'resource_request': task.get('resource_request'), 'allocation': task.get('allocation')}
