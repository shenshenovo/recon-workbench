"""SQLite metadata for the local workbench; large assets stay on disk."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
import uuid

from . import accounts
from . import evaluate
from .errors import AccessDenied

ANY_OWNER = object()
"""不按归属过滤。内部调用（恢复、导入、后台监控）使用，HTTP 层始终传入具体归属。"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def label(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 60 or '\x00' in value:
        raise ValueError('名称需要 1～60 个字符。')
    return value.strip()


class Workspace:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.quality_lock = threading.RLock()
        self.path = self.root / 'data/workbench.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS image_sets (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    name TEXT NOT NULL, path TEXT NOT NULL, input_type TEXT NOT NULL,
                    images TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL,
                    UNIQUE(project_id, path, input_type));
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    image_set_id TEXT REFERENCES image_sets(id), name TEXT NOT NULL,
                    method TEXT NOT NULL, status TEXT NOT NULL, output TEXT NOT NULL UNIQUE,
                    log TEXT, parameters TEXT NOT NULL, command TEXT NOT NULL DEFAULT '[]',
                    pid INTEGER, returncode INTEGER, error TEXT, stage TEXT, elapsed REAL,
                    created_at TEXT NOT NULL, finished_at TEXT);
                CREATE TABLE IF NOT EXISTS models (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                    kind TEXT NOT NULL, path TEXT NOT NULL, bytes INTEGER,
                    UNIQUE(task_id, path));
                CREATE INDEX IF NOT EXISTS tasks_project ON tasks(project_id, created_at);
                CREATE TABLE IF NOT EXISTS quality_reports (
                    image_set_id TEXT PRIMARY KEY REFERENCES image_sets(id),
                    report TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS image_selections (
                    image_set_id TEXT PRIMARY KEY REFERENCES image_sets(id),
                    fingerprint TEXT NOT NULL, names TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL, created_at TEXT NOT NULL, last_login_at TEXT);
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL, expires_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id, expires_at);
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(projects)')}
            if 'owner_id' not in columns:
                db.execute('ALTER TABLE projects ADD COLUMN owner_id TEXT')
            db.execute('CREATE INDEX IF NOT EXISTS projects_owner ON projects(owner_id, created_at)')
            db.execute('INSERT OR IGNORE INTO projects (id, name, created_at) VALUES (?, ?, ?)',
                       ('default', '默认项目 / 历史实验', now()))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys = ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def relative(self, value):
        try:
            return str((self.root / value).resolve().relative_to(self.root))
        except ValueError:
            raise ValueError('路径必须位于项目目录内。') from None

    # ---- 账号与会话 -------------------------------------------------------
    def count_users(self):
        with self.connect() as db:
            return db.execute('SELECT COUNT(*) FROM users').fetchone()[0]

    def user(self, user_id):
        with self.connect() as db:
            row = db.execute('SELECT id, username, created_at, last_login_at FROM users WHERE id=?',
                             (user_id,)).fetchone()
        if not row:
            raise ValueError('账号不存在。')
        return dict(row)

    def register(self, username, password):
        """创建账号，返回 (账号, 接管的历史项目数)。首个账号接管建立前的项目与历史任务。"""
        name = accounts.username(username)
        password_hash = accounts.hash_password(password)
        user_id = uuid.uuid4().hex
        first = self.count_users() == 0
        with self.connect() as db:
            if db.execute('SELECT 1 FROM users WHERE username=?', (name,)).fetchone():
                raise ValueError('该用户名已被使用。')
            db.execute('INSERT INTO users VALUES (?, ?, ?, ?, NULL)',
                       (user_id, name, password_hash, now()))
        claimed = self.claim_legacy(user_id) if first else 0
        if not claimed:
            self.create_project('我的项目', owner=user_id)
        return self.user(user_id), claimed

    def authenticate(self, username, password):
        """用户名或口令错误时返回 None，不区分两种情况，避免探测已注册用户名。"""
        name = str(username).strip() if isinstance(username, str) else ''
        with self.connect() as db:
            row = db.execute('SELECT * FROM users WHERE username=?', (name,)).fetchone()
            if not row or not accounts.verify_password(password, row['password_hash']):
                return None
            db.execute('UPDATE users SET last_login_at=? WHERE id=?', (now(), row['id']))
        return self.user(row['id'])

    def claim_legacy(self, user_id):
        """把 owner_id 为空的历史项目一次性划给首个账号，返回接管数量。"""
        with self.connect() as db:
            return db.execute('UPDATE projects SET owner_id=? WHERE owner_id IS NULL',
                             (user_id,)).rowcount or 0

    def create_session(self, user_id, days=accounts.SESSION_DAYS):
        self.user(user_id)
        token = accounts.new_token()
        with self.connect() as db:
            db.execute('DELETE FROM sessions WHERE expires_at<=?', (now(),))
            db.execute('INSERT INTO sessions VALUES (?, ?, ?, ?)',
                       (accounts.token_hash(token), user_id, now(), accounts.expiry(days)))
        return token

    def session_user(self, token):
        """令牌只以 SHA-256 摘要入库；过期的会话在这里顺手清理。"""
        if not token:
            return None
        digest = accounts.token_hash(token)
        with self.connect() as db:
            row = db.execute('''SELECT s.expires_at, u.id, u.username, u.created_at
                FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?''',
                             (digest,)).fetchone()
            if not row:
                return None
            if row['expires_at'] <= now():
                db.execute('DELETE FROM sessions WHERE token_hash=?', (digest,))
                return None
        return {'id': row['id'], 'username': row['username'], 'created_at': row['created_at']}

    def delete_session(self, token):
        if not token:
            return 0
        with self.connect() as db:
            return db.execute('DELETE FROM sessions WHERE token_hash=?',
                             (accounts.token_hash(token),)).rowcount or 0

    # ---- 项目 ------------------------------------------------------------
    def projects(self, owner=ANY_OWNER):
        query = '''SELECT p.*,
            (SELECT COUNT(*) FROM image_sets WHERE project_id=p.id) AS image_sets,
            (SELECT COUNT(*) FROM tasks WHERE project_id=p.id) AS tasks
            FROM projects p'''
        args = ()
        if owner is not ANY_OWNER:
            query += ' WHERE p.owner_id IS ?'
            args = (owner,)
        with self.connect() as db:
            return [dict(row) for row in db.execute(query + ' ORDER BY p.created_at, p.id', args)]

    def project(self, project_id, owner=ANY_OWNER):
        with self.connect() as db:
            row = db.execute('SELECT * FROM projects WHERE id=?', (project_id,)).fetchone()
        if not row:
            raise ValueError('项目不存在。')
        if owner is not ANY_OWNER and row['owner_id'] != owner:
            raise AccessDenied('项目不存在或无权访问。')
        return dict(row)

    def create_project(self, name, owner=None):
        project_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('INSERT INTO projects (id, name, created_at, owner_id) VALUES (?, ?, ?, ?)',
                       (project_id, label(name), now(), owner))
        return self.project(project_id)

    def rename_project(self, project_id, name, owner=ANY_OWNER):
        self.project(project_id, owner)
        with self.connect() as db:
            db.execute('UPDATE projects SET name=? WHERE id=?', (label(name), project_id))
        return self.project(project_id, owner)

    def register_set(self, project_id, path, input_type='images', name=None, images=None, owner=ANY_OWNER):
        self.project(project_id, owner)
        if input_type not in ('images', 'dataset', 'sfm-output'):
            raise ValueError('输入类型无效。')
        path = self.relative(path)
        if not path or not (self.root / path).is_dir():
            raise ValueError('请选择有效的输入目录。')
        with self.connect() as db:
            row = db.execute('SELECT id FROM image_sets WHERE project_id=? AND path=? AND input_type=?',
                             (project_id, path, input_type)).fetchone()
            if row:
                return self.image_set(row['id'], owner)
        if images is None:
            images = []
            if input_type in ('images', 'dataset'):
                from .prepare_brush import image_size
                image_root = self.root / path / 'images' if input_type == 'dataset' else self.root / path
                for image in sorted(image_root.rglob('*')):
                    if image.suffix.lower() in ('.jpg', '.jpeg', '.png') and image.is_file():
                        relative = self.relative(image)
                        try:
                            width, height = image_size(image)
                        except (ValueError, OSError):
                            width, height = None, None
                        images.append({'name': str(image.relative_to(image_root)), 'path': relative,
                                       'width': width, 'height': height, 'bytes': image.stat().st_size})
        set_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO image_sets VALUES (?, ?, ?, ?, ?, ?, ?)',
                       (set_id, project_id, label(name or Path(path).name), path, input_type,
                        json.dumps(images, ensure_ascii=False), now()))
            set_id = db.execute('SELECT id FROM image_sets WHERE project_id=? AND path=? AND input_type=?',
                                (project_id, path, input_type)).fetchone()['id']
        return self.image_set(set_id, owner)

    def image_set(self, set_id, owner=ANY_OWNER):
        with self.connect() as db:
            row = db.execute('''SELECT s.*, p.owner_id AS project_owner FROM image_sets s
                JOIN projects p ON p.id=s.project_id WHERE s.id=?''', (set_id,)).fetchone()
            review = db.execute('''SELECT s.names FROM quality_reports q
                LEFT JOIN image_selections s ON s.image_set_id=q.image_set_id
                WHERE q.image_set_id=?''', (set_id,)).fetchone()
        if not row:
            raise ValueError('图片集不存在。')
        if owner is not ANY_OWNER and row['project_owner'] != owner:
            raise AccessDenied('图片集不存在或无权访问。')
        result = dict(row)
        result.pop('project_owner', None)
        result['images'] = json.loads(result['images'])
        result['count'] = len(result['images'])
        result['selected_count'] = len(json.loads(review['names'])) if review and review['names'] is not None else None
        result['selection_status'] = ('not_applicable' if result['input_type'] != 'images' else
                                      'saved' if result['selected_count'] is not None else
                                      'pending' if review else 'unchecked')
        return result

    def add_image(self, set_id, image):
        with self.quality_lock, self.connect() as db:
            row = db.execute('SELECT images FROM image_sets WHERE id=?', (set_id,)).fetchone()
            if not row:
                raise ValueError('图片集不存在。')
            images = json.loads(row['images'])
            images.append(image)
            db.execute('UPDATE image_sets SET images=? WHERE id=?',
                       (json.dumps(images, ensure_ascii=False), set_id))
            db.execute('DELETE FROM quality_reports WHERE image_set_id=?', (set_id,))
            db.execute('DELETE FROM image_selections WHERE image_set_id=?', (set_id,))

    def quality_state(self, set_id, owner=ANY_OWNER):
        image_set = self.image_set(set_id, owner)
        with self.connect() as db:
            report = db.execute('SELECT * FROM quality_reports WHERE image_set_id=?', (set_id,)).fetchone()
            selection = db.execute('SELECT * FROM image_selections WHERE image_set_id=?', (set_id,)).fetchone()
        return {'image_set_id': set_id, 'supported': image_set['input_type'] == 'images',
                'report': json.loads(report['report']) if report else None,
                'analyzed_at': report['updated_at'] if report else None,
                'selection': {'fingerprint': selection['fingerprint'], 'names': json.loads(selection['names']),
                              'updated_at': selection['updated_at']} if selection else None}

    def analyze_images(self, set_id, thresholds=None, owner=ANY_OWNER):
        from .image_quality import analyze
        with self.quality_lock:
            image_set = self.image_set(set_id, owner)
            if image_set['input_type'] != 'images':
                raise ValueError('质量筛选仅用于原始照片；已有 SfM/Brush 数据不能在这里删改相机对应图片。')
            base = self.root / image_set['path']
            self.relative(base)
            images = []
            for path in sorted(base.rglob('*')):
                if path.is_file() and path.suffix.lower() in ('.jpg', '.jpeg', '.png'):
                    images.append({'name': str(path.relative_to(base)), 'path': self.relative(path),
                                   'bytes': path.stat().st_size})
                    if len(images) > 500:
                        raise ValueError('单个图片集最多检测 500 张图片。')
            report = analyze(self.root, images, thresholds)
            by_name = {item['name']: item for item in report['images']}
            for image in images:
                image.update(width=by_name[image['name']].get('width'), height=by_name[image['name']].get('height'))
            with self.connect() as db:
                db.execute('UPDATE image_sets SET images=? WHERE id=?', (json.dumps(images, ensure_ascii=False), set_id))
                db.execute('INSERT OR REPLACE INTO quality_reports VALUES (?,?,?)',
                           (set_id, json.dumps(report, ensure_ascii=False), now()))
                db.execute('DELETE FROM image_selections WHERE image_set_id=? AND fingerprint<>?',
                           (set_id, report['fingerprint']))
            return self.quality_state(set_id, owner)

    def save_selection(self, set_id, names, fingerprint, owner=ANY_OWNER):
        from .image_quality import validate_selection
        with self.quality_lock:
            state = self.quality_state(set_id, owner)
            names = validate_selection(state['report'], names, fingerprint)
            with self.connect() as db:
                db.execute('INSERT OR REPLACE INTO image_selections VALUES (?,?,?,?)',
                           (set_id, fingerprint, json.dumps(names, ensure_ascii=False), now()))
            return self.quality_state(set_id, owner)

    def image_sets(self, project_id, owner=ANY_OWNER):
        self.project(project_id, owner)
        with self.connect() as db:
            ids = [row['id'] for row in db.execute('SELECT id FROM image_sets WHERE project_id=? ORDER BY created_at', (project_id,))]
        return [self.image_set(set_id, owner) for set_id in ids]

    def create_task(self, project_id, set_id, name, method, output, log, parameters, command=(), status='starting'):
        self.project(project_id)
        if set_id and self.image_set(set_id)['project_id'] != project_id:
            raise ValueError('图片集不属于所选项目。')
        task_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('''INSERT INTO tasks
                (id,project_id,image_set_id,name,method,status,output,log,parameters,command,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                (task_id, project_id, set_id, label(name), method, status, self.relative(output),
                 self.relative(log) if log else None, json.dumps(parameters, ensure_ascii=False),
                 json.dumps(list(command)), now()))
        return self.task(task_id)

    def task(self, task_id, owner=ANY_OWNER):
        with self.connect() as db:
            row = db.execute('''SELECT t.*, p.owner_id AS project_owner FROM tasks t
                JOIN projects p ON p.id=t.project_id WHERE t.id=?''', (task_id,)).fetchone()
        if not row:
            raise ValueError('任务不存在。')
        if owner is not ANY_OWNER and row['project_owner'] != owner:
            raise AccessDenied('任务不存在或无权访问。')
        result = dict(row)
        result.pop('project_owner', None)
        for key in ('parameters', 'command'):
            result[key] = json.loads(result[key])
        result['models'] = self.models(task_id)
        return result

    def tasks(self, project_id=None, owner=ANY_OWNER):
        query = 'SELECT t.id FROM tasks t JOIN projects p ON p.id=t.project_id'
        clauses, args = [], []
        if project_id:
            self.project(project_id, owner)
            clauses.append('t.project_id=?')
            args.append(project_id)
        if owner is not ANY_OWNER:
            clauses.append('p.owner_id IS ?')
            args.append(owner)
        if clauses:
            query += ' WHERE ' + ' AND '.join(clauses)
        with self.connect() as db:
            ids = [row['id'] for row in db.execute(query + ' ORDER BY t.created_at DESC, t.id', tuple(args))]
        return [self.task(task_id) for task_id in ids]

    def update_task(self, task_id, **fields):
        allowed = {'status', 'pid', 'returncode', 'error', 'stage', 'elapsed', 'finished_at'}
        if not fields or not set(fields) <= allowed:
            raise ValueError('任务更新字段无效。')
        with self.connect() as db:
            db.execute('UPDATE tasks SET ' + ','.join(key + '=?' for key in fields) + ' WHERE id=?',
                       (*fields.values(), task_id))

    def models(self, task_id=None, project_id=None, owner=ANY_OWNER):
        query = '''SELECT m.*, t.project_id, t.name AS task_name, t.output AS task_output
            FROM models m JOIN tasks t ON m.task_id=t.id JOIN projects p ON p.id=t.project_id'''
        clauses, args = [], []
        if task_id:
            clauses.append('m.task_id=?')
            args.append(task_id)
        elif project_id:
            clauses.append('t.project_id=?')
            args.append(project_id)
        if owner is not ANY_OWNER:
            clauses.append('p.owner_id IS ?')
            args.append(owner)
        if clauses:
            query += ' WHERE ' + ' AND '.join(clauses)
        with self.connect() as db:
            return [dict(row) for row in db.execute(query + ' ORDER BY m.task_id, m.kind', tuple(args))]

    def model(self, model_id, owner=ANY_OWNER):
        """下载前取单条模型记录；权限经 task→project 链校验。"""
        with self.connect() as db:
            row = db.execute('''SELECT m.*, t.name AS task_name, t.output AS task_output,
                t.project_id, p.owner_id AS project_owner
                FROM models m JOIN tasks t ON m.task_id=t.id JOIN projects p ON p.id=t.project_id
                WHERE m.id=?''', (model_id,)).fetchone()
        if not row:
            raise ValueError('模型记录不存在。')
        if owner is not ANY_OWNER and row['project_owner'] != owner:
            raise AccessDenied('模型记录不存在或无权访问。')
        result = dict(row)
        result.pop('project_owner', None)
        return result

    def sync_task(self, task_id, status=None, code=None, error=None):
        task = self.task(task_id)
        folder = self.root / task['output']
        state = evaluate.read_json(folder / 'status.json')
        fields = {'stage': state.get('current_stage'), 'elapsed': state.get('elapsed_seconds')}
        if status:
            fields.update(status=status, returncode=code, error=state.get('error') or error)
            if status not in ('starting', 'running'):
                fields['finished_at'] = state.get('finished_at') or now()
        self.update_task(task_id, **fields)
        if (status or task['status']) == 'completed':
            try:
                row = evaluate.collect_run(folder)
            except (ValueError, OSError, KeyError):
                return
            with self.connect() as db:
                for artifact in row['artifacts']:
                    try:
                        path = self.relative(artifact['path'])
                        resolved = self.root / path
                        if folder.resolve() not in resolved.parents or not resolved.is_file():
                            continue
                    except ValueError:
                        continue
                    db.execute('INSERT OR IGNORE INTO models VALUES (?,?,?,?,?)',
                               (uuid.uuid4().hex, task_id, artifact['kind'], path, artifact['bytes']))

    def recover(self):
        """Do not reconnect to or signal old PIDs; they may have been reused."""
        for task in self.tasks():
            if task['status'] in ('starting', 'running'):
                state = evaluate.read_json(self.root / task['output'] / 'status.json')
                status = state.get('status')
                if status not in ('completed', 'failed', 'cancelled'):
                    status = 'interrupted'
                self.sync_task(task['id'], status, error=state.get('error') or
                               ('服务中断，原任务未重新接管；请检查日志后创建新任务。' if status == 'interrupted' else None))
            elif task['status'] == 'completed':
                self.sync_task(task['id'], 'completed', task['returncode'])

    def import_history(self):
        """Idempotent import; old directories and their contents remain unchanged."""
        outputs = self.root / 'outputs'
        if not outputs.is_dir():
            return
        known = {task['output'] for task in self.tasks()}
        for folder in sorted(outputs.iterdir()):
            if not folder.is_dir() or folder.is_symlink() or folder.name.startswith('.'):
                continue
            if self.relative(folder) in known:
                continue
            state = evaluate.read_json(folder / 'status.json')
            config = evaluate.read_json(folder / 'config.json')
            if not state and not (folder / 'summary.json').is_file() and not list(folder.glob('export_*.ply')):
                continue
            try:
                row = evaluate.collect_run(folder)
            except (ValueError, OSError, KeyError):
                row = {}
            status = state.get('status', 'completed' if row else 'failed')
            if status not in ('completed', 'failed', 'cancelled'):
                status = 'interrupted'
            set_id = None
            for key, input_type in (('images', 'images'), ('sfm_output', 'sfm-output'), ('dataset', 'dataset')):
                if config.get(key):
                    try:
                        set_id = self.register_set('default', config[key], input_type)['id']
                    except (ValueError, OSError):
                        pass
                    break
            log = self.root / 'outputs/.ui_logs' / (folder.name + '.log')
            task = self.create_task('default', set_id, folder.name[:60],
                                    row.get('method') or config.get('method', 'sfm'), folder,
                                    log if log.is_file() else None, config, status=status)
            created_at = state.get('started_at') or datetime.fromtimestamp(folder.stat().st_mtime, timezone.utc).isoformat(timespec='seconds')
            with self.connect() as db:
                db.execute('UPDATE tasks SET created_at=? WHERE id=?', (created_at, task['id']))
            self.sync_task(task['id'], status, error=state.get('error') or
                           ('历史运行未正常结束，需重新创建任务。' if status == 'interrupted' else None))
