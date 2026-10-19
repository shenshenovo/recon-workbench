"""SQLite metadata for the local workbench; large assets stay on disk."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
import uuid

from . import evaluate


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
            ''')
            db.execute('INSERT OR IGNORE INTO projects VALUES (?, ?, ?)',
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

    def projects(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute('''SELECT p.*,
                (SELECT COUNT(*) FROM image_sets WHERE project_id=p.id) AS image_sets,
                (SELECT COUNT(*) FROM tasks WHERE project_id=p.id) AS tasks
                FROM projects p ORDER BY created_at, id''')]

    def project(self, project_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM projects WHERE id=?', (project_id,)).fetchone()
        if not row:
            raise ValueError('项目不存在。')
        return dict(row)

    def create_project(self, name):
        project_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('INSERT INTO projects VALUES (?, ?, ?)', (project_id, label(name), now()))
        return self.project(project_id)

    def rename_project(self, project_id, name):
        self.project(project_id)
        with self.connect() as db:
            db.execute('UPDATE projects SET name=? WHERE id=?', (label(name), project_id))
        return self.project(project_id)

    def register_set(self, project_id, path, input_type='images', name=None, images=None):
        self.project(project_id)
        if input_type not in ('images', 'dataset', 'sfm-output'):
            raise ValueError('输入类型无效。')
        path = self.relative(path)
        if not path or not (self.root / path).is_dir():
            raise ValueError('请选择有效的输入目录。')
        with self.connect() as db:
            row = db.execute('SELECT id FROM image_sets WHERE project_id=? AND path=? AND input_type=?',
                             (project_id, path, input_type)).fetchone()
            if row:
                return self.image_set(row['id'])
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
        return self.image_set(set_id)

    def image_set(self, set_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM image_sets WHERE id=?', (set_id,)).fetchone()
            review = db.execute('''SELECT s.names FROM quality_reports q
                LEFT JOIN image_selections s ON s.image_set_id=q.image_set_id
                WHERE q.image_set_id=?''', (set_id,)).fetchone()
        if not row:
            raise ValueError('图片集不存在。')
        result = dict(row)
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

    def quality_state(self, set_id):
        image_set = self.image_set(set_id)
        with self.connect() as db:
            report = db.execute('SELECT * FROM quality_reports WHERE image_set_id=?', (set_id,)).fetchone()
            selection = db.execute('SELECT * FROM image_selections WHERE image_set_id=?', (set_id,)).fetchone()
        return {'image_set_id': set_id, 'supported': image_set['input_type'] == 'images',
                'report': json.loads(report['report']) if report else None,
                'analyzed_at': report['updated_at'] if report else None,
                'selection': {'fingerprint': selection['fingerprint'], 'names': json.loads(selection['names']),
                              'updated_at': selection['updated_at']} if selection else None}

    def analyze_images(self, set_id, thresholds=None):
        from .image_quality import analyze
        with self.quality_lock:
            image_set = self.image_set(set_id)
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
            return self.quality_state(set_id)

    def save_selection(self, set_id, names, fingerprint):
        from .image_quality import validate_selection
        with self.quality_lock:
            state = self.quality_state(set_id)
            names = validate_selection(state['report'], names, fingerprint)
            with self.connect() as db:
                db.execute('INSERT OR REPLACE INTO image_selections VALUES (?,?,?,?)',
                           (set_id, fingerprint, json.dumps(names, ensure_ascii=False), now()))
            return self.quality_state(set_id)

    def image_sets(self, project_id):
        self.project(project_id)
        with self.connect() as db:
            ids = [row['id'] for row in db.execute('SELECT id FROM image_sets WHERE project_id=? ORDER BY created_at', (project_id,))]
        return [self.image_set(set_id) for set_id in ids]

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

    def task(self, task_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not row:
            raise ValueError('任务不存在。')
        result = dict(row)
        for key in ('parameters', 'command'):
            result[key] = json.loads(result[key])
        result['models'] = self.models(task_id)
        return result

    def tasks(self, project_id=None):
        with self.connect() as db:
            if project_id:
                self.project(project_id)
            rows = db.execute('SELECT id FROM tasks' + (' WHERE project_id=?' if project_id else '') +
                              ' ORDER BY created_at DESC, id', (project_id,) if project_id else ())
            ids = [row['id'] for row in rows]
        return [self.task(task_id) for task_id in ids]

    def update_task(self, task_id, **fields):
        allowed = {'status', 'pid', 'returncode', 'error', 'stage', 'elapsed', 'finished_at'}
        if not fields or not set(fields) <= allowed:
            raise ValueError('任务更新字段无效。')
        with self.connect() as db:
            db.execute('UPDATE tasks SET ' + ','.join(key + '=?' for key in fields) + ' WHERE id=?',
                       (*fields.values(), task_id))

    def models(self, task_id=None, project_id=None):
        query = 'SELECT m.*, t.project_id, t.name AS task_name FROM models m JOIN tasks t ON m.task_id=t.id'
        args = ()
        if task_id:
            query += ' WHERE m.task_id=?'
            args = (task_id,)
        elif project_id:
            query += ' WHERE t.project_id=?'
            args = (project_id,)
        with self.connect() as db:
            return [dict(row) for row in db.execute(query + ' ORDER BY m.task_id, m.kind', args)]

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
