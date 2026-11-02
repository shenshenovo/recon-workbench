"""Offline, checksummed workspace backup and non-destructive restore CLI."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import sqlite3
import stat
import sys
import tarfile
import tempfile
import uuid

from .migrations import LATEST_VERSION, apply

FORMAT = 'recon-workbench-workspace-backup'
FORMAT_VERSION = 1
DEFAULT_MAX_BYTES = 100 * 1024**3
MAX_FILES = 500_000
CHUNK = 1024 * 1024


class BackupError(ValueError):
    pass


def _sha256(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(CHUNK), b''):
            result.update(block)
    return result.hexdigest()


def _safe_relative(value):
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise BackupError('归档包含非法路径。')
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ('', '.', '..') for part in value.split('/')):
        raise BackupError('归档包含绝对路径或路径穿越。')
    if path.parts[0] not in ('data', 'outputs'):
        raise BackupError('归档路径超出工作区数据范围。')
    return path


def _exclude(relative):
    parts = PurePosixPath(relative).parts
    if relative == 'data/workbench.lock':
        return True
    if relative in ('data/workbench.sqlite3-wal', 'data/workbench.sqlite3-shm',
                    'data/workbench.sqlite3-journal'):
        return True
    if any(part.endswith('.staging') or part.endswith('.tmp') for part in parts):
        return True
    if any(part in ('.restore', '.backup-tmp') for part in parts):
        return True
    return False


def _collect_files(root):
    root = Path(root)
    result = []
    for top_name in ('data', 'outputs'):
        top = root / top_name
        if not top.exists():
            continue
        if top.is_symlink() or not top.is_dir():
            raise BackupError('工作区目录无效：' + top_name)
        for directory, dirs, names in os.walk(top, followlinks=False):
            current = Path(directory)
            for name in list(dirs):
                path = current / name
                relative = path.relative_to(root).as_posix()
                if path.is_symlink():
                    raise BackupError('工作区数据不能包含符号链接：' + relative)
                if _exclude(relative):
                    dirs.remove(name)
            for name in sorted(names):
                path = current / name
                relative = path.relative_to(root).as_posix()
                if _exclude(relative):
                    continue
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode):
                    raise BackupError('工作区只允许备份普通文件：' + relative)
                # Source datasets may use hard links. Each path is serialized
                # as independent regular bytes; archive link entries remain
                # forbidden during verification/restoration.
                result.append((relative, path, info.st_size, info.st_ino, info.st_dev))
    result.sort(key=lambda row: row[0])
    if len(result) > MAX_FILES:
        raise BackupError(f'文件数量超过上限 {MAX_FILES}。')
    return result


@contextmanager
def _exclusive_workspace_lock(root):
    lock_path = Path(root) / 'data/workbench.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a+b') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError('工作区服务正在运行，请停止服务后再操作。') from None
        yield


def _check_quiescent(database):
    if not Path(database).is_file():
        raise BackupError('缺少 data/workbench.sqlite3。')
    try:
        with sqlite3.connect(Path(database).as_uri() + '?mode=ro', uri=True, timeout=5) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'tasks' not in tables:
                raise BackupError('工作区数据库缺少 tasks 表。')
            active = db.execute("SELECT id,status FROM tasks WHERE status IN ('preparing','starting','running','cancelling')").fetchall()
            if active:
                raise BackupError('仍有活动或未完成任务，拒绝备份：' + ', '.join(row[0] for row in active[:8]))
            if 'task_attempts' in tables:
                unresolved = db.execute("""SELECT id,status FROM task_attempts
                    WHERE cleanup_confirmed=0 AND status NOT IN ('queued')""").fetchall()
                if unresolved:
                    raise BackupError('存在未确认清理的执行尝试，拒绝备份。')
            if 'resource_allocations' in tables:
                allocations = db.execute("SELECT attempt_id FROM resource_allocations WHERE state='allocated'").fetchall()
                if allocations:
                    raise BackupError('存在未释放资源分配，拒绝备份。')
    except sqlite3.Error as error:
        raise BackupError('无法读取工作区数据库：' + str(error)) from error


def _sqlite_check(path, *, migrate=False):
    try:
        with sqlite3.connect(path, timeout=10) as db:
            db.execute('PRAGMA foreign_keys=ON')
            if migrate:
                apply(db)
            integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
            if integrity != 'ok':
                raise BackupError('SQLite 完整性检查失败：' + str(integrity))
            foreign = list(db.execute('PRAGMA foreign_key_check'))
            if foreign:
                raise BackupError('SQLite 外键检查失败，记录数：' + str(len(foreign)))
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'projects', 'tasks', 'models', 'image_sets'} <= tables:
                raise BackupError('SQLite 文件不是完整的工作区数据库。')
            if 'task_runtime_meta' in tables:
                row = db.execute("SELECT value FROM task_runtime_meta WHERE key='schema_version'").fetchone()
                if row and int(row[0]) > LATEST_VERSION:
                    raise BackupError('备份数据库版本高于当前程序。')
    except sqlite3.Error as error:
        raise BackupError('SQLite 数据库检查失败：' + str(error)) from error


def _check_assets(database, entries, workspace_root, read_json=None):
    """Reject a checksummed archive that omits SQLite's registered assets."""
    paths = set(entries)
    root = Path(workspace_root)

    def relative(value):
        path = Path(value)
        if path.is_absolute():
            try:
                path = path.relative_to(root)
            except ValueError:
                raise BackupError('数据库资产越出工作区：' + str(value)) from None
        return str(_safe_relative(path.as_posix()))

    def require_file(value):
        path = relative(value)
        if path not in paths:
            raise BackupError('备份缺少数据库关联资产：' + path)

    def require_tree(value):
        path = relative(value)
        if not any(item.startswith(path.rstrip('/') + '/') for item in paths):
            raise BackupError('备份缺少数据库关联目录：' + path)

    if read_json:
        for path in paths:
            if PurePosixPath(path).name != 'artifact_manifest.json':
                continue
            manifest = read_json(path)
            for item in manifest.get('files', []):
                asset = relative(str(PurePosixPath(path).parent / item['path']))
                require_file(asset)
                if entries[asset]['sha256'] != item['sha256'] or entries[asset]['bytes'] != item['bytes']:
                    raise BackupError('阶段产物与清单不一致：' + asset)

    with sqlite3.connect(database) as db:
        db.row_factory = sqlite3.Row
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for row in db.execute('SELECT path,images FROM image_sets'):
            require_tree(row['path'])
            for item in json.loads(row['images'] or '[]'):
                require_file(item['path'])
        for row in db.execute('SELECT path FROM models'):
            require_file(row['path'])
        task_columns = {row[1] for row in db.execute('PRAGMA table_info(tasks)')}
        for row in db.execute('SELECT * FROM tasks'):
            params = json.loads(row['parameters'] or '{}')
            if params.get('input_manifest'):
                require_file(params['input_manifest'])
                require_tree(params['input'])
                if read_json:
                    snapshot = read_json(relative(params['input_manifest']))
                    identity = snapshot.get('identity') or {}
                    groups = (('files', ''), ('images', 'images'), ('model', 'model'))
                    for field, suffix in groups:
                        for item in identity.get(field, []):
                            path = relative(str(Path(params['input']) / suffix / item['name']))
                            require_file(path)
                            if item.get('sha256') != entries[path]['sha256']:
                                raise BackupError('输入快照内容与数据库清单不一致：' + path)
            if params.get('quality_manifest'):
                require_file(params['quality_manifest'])
            if row['status'] == 'completed':
                require_file(str(Path(row['output']) / 'summary.json'))
                require_file(str(Path(row['output']) / 'status.json'))
            if row['log'] and 'current_attempt_id' in task_columns and row['current_attempt_id']:
                attempt = db.execute('SELECT pid FROM task_attempts WHERE id=?',
                                     (row['current_attempt_id'],)).fetchone()
                if attempt and attempt['pid']:
                    require_file(row['log'])
        if 'task_attempts' in tables:
            columns = {row[1] for row in db.execute('PRAGMA table_info(task_attempts)')}
            if 'recovery_plan' in columns:
                for row in db.execute('SELECT recovery_plan FROM task_attempts WHERE recovery_plan IS NOT NULL'):
                    for item in json.loads(row[0]).get('reuse', []):
                        require_tree(item['source_path'])


def _manifest(root, files, created_at):
    total = 0
    entries = []
    for relative, path, size, inode, device in files:
        total += size
        entries.append({'path': relative, 'bytes': size, 'sha256': _sha256(path)})
    tool_versions = []
    outputs = Path(root) / 'outputs'
    if outputs.is_dir():
        for config_path in sorted(outputs.rglob('config.json')):
            if config_path.is_symlink():
                continue
            try:
                config = json.loads(config_path.read_text(encoding='utf-8'))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(config, dict):
                continue
            keys = ('method', 'colmap', 'openmvs', 'brush', 'brush_version', 'fast3r_model', 'fast3r_size')
            tool_versions.append({'config': config_path.relative_to(root).as_posix(),
                                  'values': {key: config.get(key) for key in keys if key in config}})
    return {
        'format': FORMAT,
        'format_version': FORMAT_VERSION,
        'created_at': created_at,
        'workspace_root': str(Path(root).resolve()),
        'file_count': len(entries),
        'unpacked_bytes': total,
        'software': {'python': platform.python_version(), 'platform': platform.platform(),
                     'tool_configs': tool_versions},
        'files': entries,
    }


def create_backup(workspace, archive, max_unpacked_bytes=DEFAULT_MAX_BYTES):
    root = Path(workspace).expanduser().resolve(strict=True)
    archive = Path(archive).expanduser()
    if not archive.is_absolute():
        archive = Path.cwd() / archive
    archive = archive.parent.resolve() / archive.name
    if archive.exists() or archive.is_symlink():
        raise BackupError('备份文件已存在，不会覆盖。')
    if root == archive or root in archive.parents:
        raise BackupError('备份归档必须放在工作区之外。')
    archive.parent.mkdir(parents=True, exist_ok=True)
    with _exclusive_workspace_lock(root):
        database = root / 'data/workbench.sqlite3'
        _check_quiescent(database)
        with tempfile.TemporaryDirectory(prefix='recon-backup-db-') as temporary:
            snapshot = Path(temporary) / 'workbench.sqlite3'
            try:
                with sqlite3.connect(database) as source, sqlite3.connect(snapshot) as target:
                    source.backup(target)
            except sqlite3.Error as error:
                raise BackupError('SQLite backup API 失败：' + str(error)) from error
            _sqlite_check(snapshot)
            files = _collect_files(root)
            db_index = next((index for index, item in enumerate(files) if item[0] == 'data/workbench.sqlite3'), None)
            if db_index is None:
                raise BackupError('文件清单缺少工作区数据库。')
            old = files[db_index]
            files[db_index] = (old[0], snapshot, snapshot.stat().st_size,
                               snapshot.stat().st_ino, snapshot.stat().st_dev)
            manifest = _manifest(root, files, datetime.now(timezone.utc).isoformat(timespec='seconds'))
            _check_assets(snapshot, {item['path']: item for item in manifest['files']}, root,
                          lambda relative: json.loads((root / relative).read_text(encoding='utf-8')))
            if manifest['unpacked_bytes'] > max_unpacked_bytes:
                raise BackupError('工作区展开大小超过 --max-unpacked-bytes 上限。')
            temporary_archive = archive.parent / ('.' + archive.name + '.' + uuid.uuid4().hex + '.tmp')
            try:
                with temporary_archive.open('xb') as raw, tarfile.open(fileobj=raw, mode='w:gz') as tar:
                    for relative, path, size, inode, device in files:
                        info = path.lstat()
                        if not stat.S_ISREG(info.st_mode) or info.st_ino != inode or info.st_dev != device:
                            raise BackupError('归档期间文件身份变化：' + relative)
                        tarinfo = tarfile.TarInfo(relative)
                        tarinfo.size = size
                        tarinfo.mode = 0o600
                        tarinfo.mtime = int(info.st_mtime)
                        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
                        with os.fdopen(descriptor, 'rb') as stream:
                            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                                raise BackupError('归档期间出现非普通文件：' + relative)
                            tar.addfile(tarinfo, stream)
                    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                         separators=(',', ':')).encode('utf-8')
                    info = tarfile.TarInfo('manifest.json')
                    info.size, info.mode, info.mtime = len(encoded), 0o600, 0
                    tar.addfile(info, io.BytesIO(encoded))
                verify_archive(temporary_archive, max_unpacked_bytes)
                try:
                    os.link(temporary_archive, archive)
                except FileExistsError:
                    raise BackupError('备份文件已存在，不会覆盖。') from None
                temporary_archive.unlink()
            finally:
                temporary_archive.unlink(missing_ok=True)
    return {'archive': str(archive), 'files': manifest['file_count'],
            'unpacked_bytes': manifest['unpacked_bytes']}


def _read_manifest(tar, max_unpacked_bytes=DEFAULT_MAX_BYTES):
    members = []
    seen = set()
    manifest_member = None
    total = 0
    for member in tar:
        if len(members) >= MAX_FILES + 1:
            raise BackupError('归档文件数量超出上限。')
        if member.name in seen:
            raise BackupError('归档包含重复路径：' + member.name)
        seen.add(member.name)
        if member.issym() or member.islnk() or not member.isfile():
            raise BackupError('归档只接受普通文件：' + member.name)
        if member.name == 'manifest.json':
            manifest_member = member
            continue
        _safe_relative(member.name)
        total += member.size
        if total > max_unpacked_bytes:
            raise BackupError('归档声明的展开大小超出限制。')
        members.append(member)
    if manifest_member is None:
        raise BackupError('归档缺少 manifest.json。')
    stream = tar.extractfile(manifest_member)
    if stream is None or manifest_member.size > 16 * 1024**2:
        raise BackupError('备份清单缺失或过大。')
    try:
        manifest = json.loads(stream.read(16 * 1024**2 + 1))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise BackupError('备份清单不是有效 JSON。') from None
    if not isinstance(manifest, dict) or manifest.get('format') != FORMAT or manifest.get('format_version') != FORMAT_VERSION:
        raise BackupError('备份格式或版本不受支持。')
    entries = manifest.get('files')
    if not isinstance(entries, list) or len(entries) > MAX_FILES or manifest.get('file_count') != len(entries):
        raise BackupError('备份清单文件数量无效。')
    expected = {}
    manifest_total = 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise BackupError('备份清单条目无效。')
        relative = str(_safe_relative(entry.get('path')))
        if relative in expected:
            raise BackupError('备份清单包含重复路径：' + relative)
        size, checksum = entry.get('bytes'), entry.get('sha256')
        if isinstance(size, bool) or not isinstance(size, int) or size < 0 or not isinstance(checksum, str) or len(checksum) != 64:
            raise BackupError('备份清单大小或哈希无效：' + relative)
        expected[relative] = entry
        manifest_total += size
    if set(expected) != {member.name for member in members} or manifest_total != manifest.get('unpacked_bytes'):
        raise BackupError('归档成员与清单不一致。')
    return manifest, members, expected


def verify_archive(archive, max_unpacked_bytes=DEFAULT_MAX_BYTES):
    total = 0
    database_temp = tempfile.NamedTemporaryFile(prefix='recon-verify-db-', delete=False)
    database_path = Path(database_temp.name)
    database_temp.close()
    database_seen = False
    try:
        with tarfile.open(archive, mode='r:gz') as tar:
            manifest, members, expected = _read_manifest(tar, max_unpacked_bytes)
            if manifest['unpacked_bytes'] > max_unpacked_bytes:
                raise BackupError('归档展开大小超过 --max-unpacked-bytes 上限。')
            by_name = {member.name: member for member in members}
            for relative, entry in expected.items():
                member = by_name[relative]
                if member.size != entry['bytes']:
                    raise BackupError('归档文件大小与清单不一致：' + relative)
                stream = tar.extractfile(member)
                if stream is None:
                    raise BackupError('归档文件无法读取：' + relative)
                hasher = hashlib.sha256()
                target = database_path.open('wb') if relative == 'data/workbench.sqlite3' else None
                try:
                    while True:
                        block = stream.read(CHUNK)
                        if not block:
                            break
                        total += len(block)
                        if total > max_unpacked_bytes:
                            raise BackupError('实际展开大小超过限制。')
                        hasher.update(block)
                        if target:
                            target.write(block)
                finally:
                    if target:
                        target.close()
                if hasher.hexdigest() != entry['sha256']:
                    raise BackupError('SHA-256 校验失败：' + relative)
                if relative == 'data/workbench.sqlite3':
                    database_seen = True
        if not database_seen:
            raise BackupError('备份中缺少 data/workbench.sqlite3。')
        _sqlite_check(database_path)
        if not isinstance(manifest.get('workspace_root'), str) or not Path(manifest['workspace_root']).is_absolute():
            raise BackupError('备份缺少原工作区绝对路径。')
        _check_quiescent(database_path)
        with tarfile.open(archive, mode='r:gz') as tar:
            def read_json(relative):
                member = tar.getmember(relative)
                if member.size > 16 * 1024**2:
                    raise BackupError('资产 JSON 超过 16 MiB 限制。')
                return json.load(tar.extractfile(member))
            _check_assets(database_path, expected, manifest['workspace_root'], read_json)
        return manifest
    except (tarfile.TarError, OSError, EOFError, ValueError, KeyError, TypeError) as error:
        if isinstance(error, BackupError):
            raise
        raise BackupError('归档读取失败：' + str(error)) from error
    finally:
        database_path.unlink(missing_ok=True)


def _replace_prefix(value, old, new):
    if isinstance(value, str):
        if value == old:
            return new
        prefix = old.rstrip(os.sep) + os.sep
        if value.startswith(prefix):
            return new.rstrip(os.sep) + os.sep + value[len(prefix):]
        return value
    if isinstance(value, list):
        return [_replace_prefix(item, old, new) for item in value]
    if isinstance(value, dict):
        return {key: item if key in ('identity', 'submission_identity', 'command', 'training_command')
                else _replace_prefix(item, old, new) for key, item in value.items()}
    return value


def _rewrite_json_files(root, old, new):
    known_names = {'config.json', 'status.json', 'summary.json', 'dataset.json',
                   'input.json', 'selection.json', 'quality.json', 'evaluation.json', 'split.json'}
    for top in (Path(root) / 'data', Path(root) / 'outputs'):
        if not top.is_dir():
            continue
        for path in top.rglob('*.json'):
            if path.name not in known_names:
                continue
            if path.is_symlink() or not path.is_file():
                raise BackupError('恢复 JSON 路径不是普通文件：' + str(path))
            try:
                value = json.loads(path.read_text(encoding='utf-8'))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            rewritten = _replace_prefix(value, old, new)
            if rewritten != value:
                temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
                try:
                    temp.write_text(json.dumps(rewritten, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
                    temp.replace(path)
                finally:
                    temp.unlink(missing_ok=True)


def _refresh_attempt_manifests(root):
    from .validators import capture_output_manifests
    database = Path(root) / 'data/workbench.sqlite3'
    with sqlite3.connect(database) as db:
        paths = {row[0] for row in db.execute('SELECT output FROM tasks WHERE output IS NOT NULL')}
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'task_attempts' in tables:
            paths.update(row[0] for row in db.execute(
                'SELECT output_path FROM task_attempts WHERE output_path IS NOT NULL'))
    candidates = {Path(root) / relative for relative in paths}
    roots = [candidate for candidate in candidates
             if not any(other != candidate and candidate.is_relative_to(other) for other in candidates)]
    for candidate in sorted(roots, key=lambda item: str(item)):
        if candidate.is_dir() and not candidate.is_symlink():
            capture_output_manifests(root, candidate)


def _rewrite_database(root, old, new):
    database = Path(root) / 'data/workbench.sqlite3'
    with sqlite3.connect(database, timeout=10) as db:
        db.execute('PRAGMA foreign_keys=ON')
        apply(db)
        columns = lambda table: {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
        for table, fields in (
            ('tasks', ('output', 'output_root', 'log')),
            ('models', ('path',)), ('image_sets', ('path',)),
            ('task_attempts', ('output_path', 'log_path')),
        ):
            available = columns(table)
            for field in fields:
                if field in available:
                    db.execute(f'UPDATE {table} SET {field}=? WHERE {field}=?', (new, old))
                    db.execute(f'UPDATE {table} SET {field}=replace({field},?,?) WHERE instr({field},?)>0',
                               (old.rstrip(os.sep) + os.sep, new.rstrip(os.sep) + os.sep,
                                old.rstrip(os.sep) + os.sep))
        for table, field in (('tasks', 'parameters'), ('image_sets', 'images'),
                             ('quality_reports', 'report'), ('task_attempts', 'recovery_plan')):
            if field not in columns(table):
                continue
            for rowid, value in db.execute(f'SELECT rowid,{field} FROM {table} WHERE {field} IS NOT NULL').fetchall():
                try:
                    parsed = json.loads(value)
                except (TypeError, json.JSONDecodeError):
                    continue
                rewritten = _replace_prefix(parsed, old, new)
                if rewritten != parsed:
                    db.execute(f'UPDATE {table} SET {field}=? WHERE rowid=?',
                               (json.dumps(rewritten, ensure_ascii=False), rowid))
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'tasks' in tables:
            # Archived commands are retained for audit. They must never be
            # dispatched on a different host: explicit task recovery rebuilds
            # a command from validated parameters and the current interpreter.
            db.execute("UPDATE tasks SET status='interrupted',error='离线恢复：请重新预检并恢复该排队任务。' "
                       "WHERE status='queued'")
            db.execute("UPDATE tasks SET pid=NULL WHERE status NOT IN ('starting','running','cancelling')")
        if 'task_attempts' in tables:
            db.execute("UPDATE task_attempts SET status='interrupted',cleanup_confirmed=1 "
                       "WHERE status='queued'")
            db.execute("UPDATE task_attempts SET pid=NULL,pgid=NULL,process_start=NULL "
                       "WHERE status NOT IN ('starting','running','cancelling')")
            # Root relocation changes absolute parameters and native tool
            # dependencies. Historical fingerprints remain evidence, but are
            # not sufficient to reuse a stage on the restored machine.
            db.execute('UPDATE task_attempts SET execution_fingerprint=NULL,parameters_fingerprint=NULL')
        db.execute("INSERT INTO task_runtime_meta(key,value) VALUES ('dispatch_paused','1') "
                   "ON CONFLICT(key) DO UPDATE SET value='1'")
        db.commit()
    _sqlite_check(database)


def _refresh_input_snapshots(root, final_root):
    """Rehash only input JSON whose known absolute paths were relocated."""
    from .snapshots import digest
    root, final_root = Path(root), Path(final_root)
    with sqlite3.connect(root / 'data/workbench.sqlite3') as db:
        def local(value):
            path = Path(value)
            if path.is_absolute():
                path = path.relative_to(final_root)
            return root.joinpath(*_safe_relative(path.as_posix()).parts)
        for task_id, raw in db.execute('SELECT id,parameters FROM tasks').fetchall():
            params = json.loads(raw)
            if not params.get('input_manifest'):
                continue
            manifest_path = local(params['input_manifest'])
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            identity = manifest['identity']
            base = local(params['input'])
            for key, suffix in (('files', ''), ('images', 'images'), ('model', 'model')):
                for item in identity.get(key, []):
                    path = base / suffix / item['name']
                    item['sha256'] = _sha256(path)
                    if 'bytes' in item:
                        item['bytes'] = path.stat().st_size
            manifest['fingerprint'] = digest(identity)
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            params['input_fingerprint'] = manifest['fingerprint']
            db.execute('UPDATE tasks SET input_fingerprint=?,parameters=? WHERE id=?',
                       (manifest['fingerprint'], json.dumps(params, ensure_ascii=False), task_id))


def restore_backup(archive, target, max_unpacked_bytes=DEFAULT_MAX_BYTES):
    archive = Path(archive).expanduser().resolve(strict=True)
    target = Path(target).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    if any(path.is_symlink() for path in (target, *target.parents)):
        raise BackupError('恢复目标不能经过符号链接。')
    target = target.parent.resolve() / target.name
    if target.exists() or target.is_symlink():
        raise BackupError('恢复目标必须是不存在的新目录。')
    parent = target.parent
    if not parent.is_dir() or any(part.is_symlink() for part in (parent, *parent.parents)):
        raise BackupError('恢复目标父目录必须存在且不能经过符号链接。')
    manifest = verify_archive(archive, max_unpacked_bytes)
    if shutil.disk_usage(parent).free < manifest['unpacked_bytes'] * 1.1:
        raise BackupError('恢复目标可用磁盘空间不足。')
    staging = parent / ('.' + target.name + '.' + uuid.uuid4().hex + '.restore')
    staging.mkdir(mode=0o700)
    try:
        with tarfile.open(archive, mode='r:gz') as tar:
            checked_manifest, members, expected = _read_manifest(tar, max_unpacked_bytes)
            if checked_manifest != manifest:
                raise BackupError('归档在验证后发生变化。')
            written_bytes = 0
            for member in members:
                relative = _safe_relative(member.name)
                destination = staging.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists() or destination.is_symlink():
                    raise BackupError('恢复目标出现重复路径：' + member.name)
                source = tar.extractfile(member)
                if source is None:
                    raise BackupError('归档成员无法读取：' + member.name)
                record = expected[member.name]
                hasher = hashlib.sha256()
                with destination.open('xb') as output:
                    while True:
                        block = source.read(CHUNK)
                        if not block:
                            break
                        written_bytes += len(block)
                        if written_bytes > max_unpacked_bytes:
                            raise BackupError('恢复展开大小超过限制。')
                        hasher.update(block)
                        output.write(block)
                if destination.stat().st_size != record['bytes'] or hasher.hexdigest() != record['sha256']:
                    raise BackupError('恢复文件校验失败：' + member.name)
        _rewrite_database(staging, manifest['workspace_root'], str(target))
        _rewrite_json_files(staging, manifest['workspace_root'], str(target))
        _refresh_input_snapshots(staging, target)
        _refresh_attempt_manifests(staging)
        if target.exists() or target.is_symlink():
            raise BackupError('恢复目标在操作期间被创建。')
        os.rename(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {'workspace': str(target), 'files': manifest['file_count'], 'dispatch_paused': True}


def _positive_limit(value):
    number = int(value)
    if number <= 0 or number > 1024 * 1024**4:
        raise argparse.ArgumentTypeError('必须是 1 到 1 PiB 之间的正整数。')
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description='工作区离线备份、校验与新目录恢复。')
    sub = parser.add_subparsers(dest='action', required=True)
    create = sub.add_parser('create')
    create.add_argument('--workspace', required=True, type=Path)
    create.add_argument('--archive', required=True, type=Path)
    create.add_argument('--max-unpacked-bytes', type=_positive_limit, default=DEFAULT_MAX_BYTES)
    verify = sub.add_parser('verify')
    verify.add_argument('--archive', required=True, type=Path)
    verify.add_argument('--max-unpacked-bytes', type=_positive_limit, default=DEFAULT_MAX_BYTES)
    restore = sub.add_parser('restore')
    restore.add_argument('--archive', required=True, type=Path)
    restore.add_argument('--target', required=True, type=Path)
    restore.add_argument('--max-unpacked-bytes', type=_positive_limit, default=DEFAULT_MAX_BYTES)
    args = parser.parse_args(argv)
    try:
        if args.action == 'create':
            result = create_backup(args.workspace, args.archive, args.max_unpacked_bytes)
            print(f"备份已验证：{result['archive']}（{result['files']} 个文件，{result['unpacked_bytes']} 字节）")
        elif args.action == 'verify':
            manifest = verify_archive(args.archive, args.max_unpacked_bytes)
            print(f"备份有效：{manifest['file_count']} 个文件，{manifest['unpacked_bytes']} 字节")
        else:
            result = restore_backup(args.archive, args.target, args.max_unpacked_bytes)
            print(f"工作区已恢复：{result['workspace']}；派发保持暂停，请核对后再恢复队列。")
        return 0
    except KeyboardInterrupt:
        print('备份操作已中断，未发布的临时文件已清理。', file=sys.stderr)
        return 130
    except (BackupError, OSError, sqlite3.Error, tarfile.TarError, ValueError) as error:
        print('备份操作失败：' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
