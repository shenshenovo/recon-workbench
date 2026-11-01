"""工作区运行时的事务化、增量迁移；不重建或删除历史表。"""
import sqlite3

LATEST_VERSION = 1


def _add_column(db, table, name, definition):
    if name not in {row[1] for row in db.execute(f'PRAGMA table_info({table})')}:
        db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')


def _migration_1(db):
    for name, definition in (
        ('queue_seq', 'INTEGER'), ('idempotency_key', 'TEXT'),
        ('request_fingerprint', 'TEXT'), ('input_fingerprint', 'TEXT'),
        ('cancel_requested_at', 'TEXT'), ('current_attempt_id', 'TEXT'),
    ):
        _add_column(db, 'tasks', name, definition)
    db.execute('''CREATE TABLE task_queue_sequence (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id))''')
    db.execute('''CREATE TABLE task_attempts (
        id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
        attempt_no INTEGER NOT NULL, status TEXT NOT NULL,
        command TEXT NOT NULL, pid INTEGER, pgid INTEGER, process_start TEXT,
        started_at TEXT NOT NULL, finished_at TEXT, returncode INTEGER, error TEXT,
        cancel_requested_at TEXT, cleanup_confirmed INTEGER NOT NULL DEFAULT 0,
        UNIQUE(task_id, attempt_no))''')
    db.execute('CREATE INDEX task_queue_order ON tasks(status, queue_seq)')
    db.execute('''CREATE UNIQUE INDEX task_idempotency ON tasks(project_id, idempotency_key)
        WHERE idempotency_key IS NOT NULL''')
    db.execute('''CREATE UNIQUE INDEX task_active_attempt ON task_attempts(task_id)
        WHERE cleanup_confirmed=0''')


def apply(db):
    # SAVEPOINT composes with Workspace's existing transaction and protects
    # DDL as well as the version marker if any individual step fails.
    db.execute('SAVEPOINT task_runtime_migration')
    try:
        db.execute('CREATE TABLE IF NOT EXISTS task_runtime_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        row = db.execute("SELECT value FROM task_runtime_meta WHERE key='schema_version'").fetchone()
        current = int(row[0]) if row else 0
        if current > LATEST_VERSION:
            raise ValueError('运行时数据库版本高于当前程序，拒绝降级打开。')
        if current == 0:
            _migration_1(db)
            db.execute("INSERT INTO task_runtime_meta VALUES ('schema_version', '1')")
        db.execute('RELEASE task_runtime_migration')
        return LATEST_VERSION
    except BaseException:
        db.execute('ROLLBACK TO task_runtime_migration')
        db.execute('RELEASE task_runtime_migration')
        raise


def migrate_path(path):
    """升级已经由 Workspace 建立的数据库，不创建第二套工作区模式。"""
    with sqlite3.connect(path, timeout=10) as db:
        return apply(db)
