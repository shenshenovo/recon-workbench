#!/usr/bin/env python3
"""Loopback web UI for local reconstruction, image review and project records."""
import argparse
from contextlib import nullcontext
import fcntl
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
import re
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import threading
from urllib.parse import parse_qs, urlparse
import webbrowser
from .prepare_brush import image_size
from .pointcloud import cloud_packet
from .workspace import Workspace
from .paths import PROJECT_ROOT
from . import image_quality

from . import evaluate
from . import pipeline as routes

ROOT = PROJECT_ROOT


def project_path(value):
    path = (ROOT / value).resolve()
    if path != ROOT and ROOT not in path.parents:
        raise ValueError("路径必须位于项目目录内。")
    return path


def job_arguments(payload, output):
    method = payload.get("method", "3dgs")
    input_type = payload.get("input_type", "dataset")
    if input_type not in ("images", "sfm-output", "dataset"):
        raise ValueError("输入类型无效。")
    source = project_path(payload.get("input", ""))
    if not source.is_dir() or source == ROOT:
        raise ValueError("请选择有效的输入目录。")
    argv = ["--method", method, "--" + input_type, str(source), "--output", str(output)]
    values = {"steps": (5000, 1, 1000000), "max-image-size": (1200, 64, 8192),
              "max-splats": (1000000, 1, 10000000), "threads": (4, 1, 32),
              "eval-every": (1000, 1, 1000000), "export-every": (1000, 1, 1000000),
              "seed": (42, 0, 2**32 - 1)}
    for name, (default, minimum, maximum) in values.items():
        value = int(payload.get(name, default))
        if not minimum <= value <= maximum:
            raise ValueError("{} 超出范围。".format(name))
        argv.extend(["--" + name, str(value)])
    argv.extend(["--camera-model", payload.get("camera_model", "PINHOLE"),
                 "--mvs-stage", payload.get("mvs_stage", "textured")])
    if input_type == "images":
        if payload.get("camera_params"):
            if not isinstance(payload["camera_params"], str) or len(payload["camera_params"]) > 1024:
                raise ValueError("相机内参必须是英文逗号分隔的数字，长度不能超过 1024。")
            argv.extend(["--camera-params", payload["camera_params"]])
        if payload.get("single_camera"):
            argv.append("--single-camera")
    if method == "3dgs":
        every = int(payload.get("eval_split_every", 8))
        if every:
            if every < 2:
                raise ValueError("评估间隔至少为 2，0 表示不评估。")
            argv.extend(["--eval-split-every", str(every)])
            if payload.get("save_eval_images"):
                argv.append("--save-eval-images")
        if payload.get("with_viewer"):
            argv.append("--with-viewer")
    args = routes.get_parser().parse_args(argv)
    routes.build_plan(args, output)
    if source == output or source in output.parents or output in source.parents:
        raise ValueError("输出与输入目录重叠。")
    return argv


def tail_log(path, size=16000):
    try:
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - size))
            return evaluate.clean_terminal(stream.read().decode("utf-8", errors="replace"))
    except OSError:
        return ""


class Jobs:
    def __init__(self, store=None):
        self.store = store
        self.lock = threading.Lock()
        self.process = None
        self.output = None
        self.log = None
        self.cancel_requested = False
        self.task_id = None
        self.worker = None

    def start(self, payload):
        with self.lock:
            if self.process and self.process.poll() is None:
                raise ValueError("已有任务运行中，请等待完成或停止它。")
            label = payload.get("name", "experiment").strip() or "experiment"
            if len(label) > 60 or any(char in label for char in '/\\\x00') or label in (".", ".."):
                raise ValueError("实验名称无效。")
            payload = dict(payload)
            project_id = payload.get('project_id', 'default')
            image_set = None
            if self.store:
                self.store.project(project_id)
                if payload.get('image_set_id'):
                    image_set = self.store.image_set(payload['image_set_id'])
                    if image_set['project_id'] != project_id:
                        raise ValueError('图片集不属于所选项目。')
                    payload.update(input=image_set['path'], input_type=image_set['input_type'])
            output = ROOT / "outputs" / (label + "_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
            argv = job_arguments(payload, output)
            if self.store and image_set is None:
                image_set = self.store.register_set(project_id, payload['input'], payload.get('input_type', 'dataset'))
            quality = None
            if self.store and image_set['input_type'] == 'images':
                quality = self.store.quality_state(image_set['id'])
                if not quality['report'] or not quality['selection']:
                    raise ValueError('请先检测图片质量并保存筛选结果，再开始重建。')
                names = image_quality.validate_selection(quality['report'], quality['selection']['names'],
                                                         quality['selection']['fingerprint'])
                snapshot_path = ROOT / 'data/task_inputs' / output.name / 'images'
                payload.update(source_input=payload['input'], input=str(snapshot_path.relative_to(ROOT)),
                               image_set_id=image_set['id'], selected_images=names,
                               quality_fingerprint=quality['report']['fingerprint'],
                               quality_manifest=str((snapshot_path.parent / 'selection.json').relative_to(ROOT)))
                argv[argv.index('--images') + 1] = str(snapshot_path)
            logs = ROOT / "outputs/.ui_logs"
            logs.mkdir(parents=True, exist_ok=True)
            log = logs / (output.name + ".log")
            command = [sys.executable, "-u", str(ROOT / "main.py")] + argv
            task = self.store.create_task(project_id, image_set['id'], label, payload.get('method', '3dgs'),
                                          output, log, payload, command) if self.store else None
            try:
                if quality:
                    with self.store.quality_lock:
                        current_quality = self.store.quality_state(image_set['id'])
                        if current_quality['selection'] != quality['selection'] or current_quality['report'] != quality['report']:
                            raise ValueError('图片集或筛选结果已变化，请重新确认后启动。')
                        files = image_quality.snapshot(ROOT, quality['report'], names, snapshot_path)
                    (snapshot_path.parent / 'selection.json').write_text(json.dumps(
                        {'image_set_id': image_set['id'], 'fingerprint': quality['report']['fingerprint'],
                         'policy': quality['report']['policy'], 'files': files}, ensure_ascii=False, indent=2), encoding='utf-8')
                    (snapshot_path.parent / 'quality.json').write_text(json.dumps(quality['report'], ensure_ascii=False, indent=2), encoding='utf-8')
                with log.open("w", encoding="utf-8") as stream:
                    self.process = subprocess.Popen(command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT,
                                                    start_new_session=True)
            except (OSError, ValueError) as error:
                if task:
                    self.store.sync_task(task['id'], 'failed', error=str(error))
                raise
            self.output, self.log, self.cancel_requested = output, log, False
            self.task_id = task['id'] if task else None
            if task:
                self.store.update_task(task['id'], status='running', pid=self.process.pid)
                self.worker = threading.Thread(target=self.monitor, args=(self.process, task['id']), daemon=True)
                self.worker.start()
            return {"output": str(output.relative_to(ROOT)), "pid": self.process.pid, 'task_id': self.task_id}

    def monitor(self, process, task_id):
        code = process.wait()
        with self.lock:
            cancelled = self.task_id == task_id and self.cancel_requested
            status = 'completed' if code == 0 else 'cancelled' if code in (130, -signal.SIGINT) or cancelled else 'failed'
            self.store.sync_task(task_id, status, code,
                                 error=None if code == 0 else '用户停止任务。' if status == 'cancelled' else '任务执行失败，请查看日志。')

    def close(self):
        self.cancel()
        if self.process and self.process.poll() is None:
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        if self.worker:
            self.worker.join(timeout=5)

    def cancel(self):
        with self.lock:
            if self.process and self.process.poll() is None:
                try:
                    os.killpg(self.process.pid, signal.SIGINT)
                    self.cancel_requested = True
                except ProcessLookupError:
                    pass
            return {"cancel_requested": self.cancel_requested}

    def snapshot(self):
        with self.lock:
            if not self.process:
                return {"status": "idle", "log": "", "states": []}
            code = self.process.poll()
            states = []
            if self.output.exists():
                for path in sorted(self.output.rglob("status.json")):
                    state = evaluate.read_json(path)
                    states.append({"path": str(path.relative_to(self.output)), "status": state.get("status"),
                                   "stage": state.get("current_stage"), "elapsed": state.get("elapsed_seconds"),
                                   "error": state.get("error")})
            log = tail_log(self.log)
            for relative in ("3dgs/logs/03_train.log", "mvs/logs/08_dense.log", "sfm/logs/03_mapper.log"):
                candidate = self.output / relative
                if candidate.exists():
                    log += "\n" + relative + "\n" + tail_log(candidate)
            progress = list(re.finditer(r"(\d+)\s*/\s*(\d+)\s+Steps", log))
            last = progress[-1] if progress else None
            if self.store and self.task_id:
                self.store.sync_task(self.task_id)
            return {"status": "running" if code is None else "completed" if code == 0 else "cancelled" if code in (130, -signal.SIGINT) or self.cancel_requested else "failed",
                    "returncode": code, "output": str(self.output.relative_to(ROOT)), "states": states, 'task_id': self.task_id,
                    "progress": {"step": int(last[1]), "total": int(last[2])} if last else None,
                    "log": log[-24000:]}


class Uploads:
    def __init__(self, store=None):
        self.store = store
        self.sessions = {}
        self.lock = threading.Lock()

    def create(self, project_id='default', name=None):
        if self.store:
            self.store.project(project_id)
        key = secrets.token_hex(12)
        path = ROOT / 'data/uploads' / key
        path.mkdir(parents=True)
        self.sessions[key] = {'path': path, 'files': [], 'bytes': 0}
        if self.store:
            image_set = self.store.register_set(project_id, path, name=name or '上传照片', images=[])
            self.sessions[key]['image_set_id'] = image_set['id']
        else:
            image_set = None
        return {'id': key, 'path': str(path.relative_to(ROOT)), 'image_set_id': image_set['id'] if image_set else None}

    def receive(self, key, name, stream, size):
        with self.lock, self.store.quality_lock if self.store else nullcontext():
            session = self.sessions.get(key)
            if not session:
                raise ValueError('上传批次不存在，请重新选择图片。')
            if not name or Path(name).name != name or '\\' in name or name.startswith('.') or '\x00' in name:
                raise ValueError('图片名称无效。')
            if Path(name).suffix.lower() not in ('.jpg', '.jpeg', '.png'):
                raise ValueError('当前支持 JPG/JPEG/PNG 图片。')
            if len(session['files']) >= 500 or not 0 < size <= 64 * 1024**2 or session['bytes'] + size > 1024**3:
                raise ValueError('最多上传 500 张、单张 64 MB、单批 1 GB。')
            path = session['path'] / name
            if path.exists():
                raise ValueError('同批图片名称重复：' + name)
            try:
                with path.open('xb') as out:
                    remaining = size
                    while remaining:
                        block = stream.read(min(remaining, 1024**2))
                        if not block:
                            raise ValueError('上传文件截断。')
                        out.write(block)
                        remaining -= len(block)
                width, height = image_size(path)
                if min(width, height) <= 0:
                    raise ValueError('图片尺寸无效。')
                if self.store:
                    self.store.add_image(session['image_set_id'], {'name': name, 'path': str(path.relative_to(ROOT)),
                                                                  'width': width, 'height': height, 'bytes': size})
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            session['files'].append(name)
            session['bytes'] += size
            return {'files': len(session['files']), 'path': str(session['path'].relative_to(ROOT))}


def records(store=None, project_id=None):
    rows = []
    for folder in evaluate.discover_runs(ROOT / "outputs"):
        try:
            row = evaluate.collect_run(folder)
            row["id"] = str(folder.relative_to(ROOT))
            rows.append(row)
        except (ValueError, OSError, KeyError):
            continue
    if store:
        tasks = {task['output']: task for task in store.tasks(project_id)}
        rows = [row for row in rows if row['id'] in tasks and tasks[row['id']]['status'] == 'completed']
        for row in rows:
            row.update(task_id=tasks[row['id']]['id'], project_id=tasks[row['id']]['project_id'])
    return rows


def task_detail(store, task_id):
    task = store.task(task_id)
    folder = project_path(task['output'])
    task['states'] = []
    if folder.is_dir():
        for path in sorted(folder.rglob('status.json')):
            state = evaluate.read_json(path)
            task['states'].append({'path': str(path.relative_to(folder)), 'status': state.get('status'),
                                   'stage': state.get('current_stage'), 'error': state.get('error')})
    task['log_text'] = tail_log(project_path(task['log'])) if task['log'] else ''
    for relative in ('logs/01_sfm.log', 'logs/02_mvs.log', 'logs/02_3dgs.log',
                     '3dgs/logs/03_train.log', 'mvs/logs/08_dense.log', 'sfm/logs/03_mapper.log'):
        path = folder / relative
        if path.is_file():
            task['log_text'] += '\n' + relative + '\n' + tail_log(path)
    task['log_text'] = task['log_text'][-48000:]
    return task


class Handler(BaseHTTPRequestHandler):
    def respond(self, body, code=200, content_type="application/json; charset=utf-8"):
        data = json.dumps(body, ensure_ascii=False).encode() if not isinstance(body, bytes) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def valid_host(self):
        return self.headers.get("Host") in ("127.0.0.1:" + str(self.server.server_port), "localhost:" + str(self.server.server_port))

    def do_GET(self):
        if not self.valid_host():
            return self.respond({"error": "无效 Host"}, 403)
        url = urlparse(self.path)
        try:
            pages = {'/': 'index.html', '/reconstruct': 'index.html', '/projects': 'projects.html', '/experiments': 'experiments.html'}
            if url.path in pages:
                data = (ROOT / 'web' / pages[url.path]).read_text().replace("__TOKEN__", self.server.token)
                return self.respond(data.encode(), content_type="text/html; charset=utf-8")
            assets = {'/styles.css', '/common.js', '/workbench.js', '/projects.js', '/quality.js', '/experiments.js', '/viewer.js'}
            if url.path in assets:
                return self.respond((ROOT / 'web' / url.path[1:]).read_bytes(), content_type='text/css; charset=utf-8' if url.path.endswith('.css') else 'text/javascript; charset=utf-8')
            query = parse_qs(url.query)
            if url.path == '/api/projects':
                return self.respond(self.server.store.projects())
            if url.path == '/api/project':
                project_id = query.get('id', ['default'])[0]
                project = self.server.store.project(project_id)
                return self.respond({**project, 'image_sets': self.server.store.image_sets(project_id),
                                     'tasks': self.server.store.tasks(project_id),
                                     'models': self.server.store.models(project_id=project_id)})
            if url.path == '/api/tasks':
                return self.respond(self.server.store.tasks(query.get('project_id', [None])[0]))
            if url.path == '/api/quality':
                return self.respond(self.server.store.quality_state(query.get('image_set_id', [''])[0]))
            if url.path == '/api/task':
                return self.respond(task_detail(self.server.store, query.get('id', [''])[0]))
            if url.path == '/api/source-image':
                image_set = self.server.store.image_set(query.get('set', [''])[0])
                image = next((image for image in image_set['images'] if image['name'] == query.get('name', [''])[0]), None)
                if not image:
                    raise ValueError('图片不在此图片集中。')
                path = project_path(image['path'])
                return self.respond(path.read_bytes(), content_type=mimetypes.guess_type(path.name)[0])
            if url.path == "/api/job":
                return self.respond(self.server.jobs.snapshot())
            if url.path == "/api/runs":
                return self.respond(records(self.server.store, query.get('project_id', [None])[0]))
            if url.path == '/viewer.js':
                return self.respond((ROOT / 'web/viewer.js').read_bytes(), content_type='text/javascript; charset=utf-8')
            if url.path == '/api/cloud':
                query = parse_qs(url.query)
                row = next((row for row in records(self.server.store) if row['id'] == query.get('run', [''])[0]), None)
                if not row:
                    raise ValueError('未知任务。')
                kinds = ('sparse_pointcloud', 'pointcloud') if query.get('kind', ['dense'])[0] == 'sparse' else ('colmap_pointcloud', 'dense_pointcloud', 'pointcloud')
                artifact = next((item for kind in kinds for item in row['artifacts'] if item['kind'] == kind and item['path'].endswith('.ply')), None)
                if not artifact or row['method'] == '3dgs':
                    raise ValueError('该任务没有普通点云；3DGS 高斯模型仍使用 Brush 查看。')
                return self.respond(cloud_packet(project_path(artifact['path'])), content_type='application/octet-stream')
            if url.path == "/api/folders":
                path = project_path(parse_qs(url.query).get("path", ["data"])[0])
                folders = []
                for child in sorted(path.iterdir()):
                    if child.is_dir() and not child.name.startswith('.'):
                        try:
                            relative = child.resolve().relative_to(ROOT)
                        except ValueError:
                            continue
                        folders.append({"name": child.name, "path": str(relative)})
                return self.respond({"path": str(path.relative_to(ROOT)), "folders": folders})
            if url.path == "/api/image":
                path = project_path(parse_qs(url.query).get("path", [""])[0])
                if path.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                    raise ValueError("只提供图片预览。")
                if not (ROOT / "outputs") in path.parents:
                    raise ValueError("仅提供结果目录内的图片。")
                return self.respond(path.read_bytes(), content_type=mimetypes.guess_type(path.name)[0])
            if url.path == "/api/previews":
                folder = project_path(parse_qs(url.query).get("run", [""])[0])
                if folder not in evaluate.discover_runs(ROOT / "outputs"):
                    raise ValueError("未知任务。")
                images = [str(path.relative_to(ROOT)) for path in folder.rglob('*')
                          if path.suffix.lower() in (".jpg", ".png", ".jpeg") and 'gaussians' in path.parts]
                return self.respond(images[:100])
            return self.respond({"error": "Not found"}, 404)
        except (ValueError, OSError) as error:
            self.respond({"error": str(error)}, 400)

    def do_POST(self):
        if not self.valid_host() or self.headers.get("X-Local-Token") != self.server.token:
            return self.respond({"error": "请求校验失败"}, 403)
        try:
            size = int(self.headers.get("Content-Length", 0))
            if urlparse(self.path).path == '/api/upload-file':
                query = parse_qs(urlparse(self.path).query)
                result = self.server.uploads.receive(query.get('id', [''])[0], query.get('name', [''])[0], self.rfile, size)
                return self.respond(result)
            if not 0 < size <= (1024**2 if self.path == '/api/image-selection' else 65536):
                raise ValueError("请求大小无效。")
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError("请求需要 JSON 对象。")
            if self.path == '/api/upload-new':
                result = self.server.uploads.create(payload.get('project_id', 'default'), payload.get('name'))
            elif self.path == '/api/projects':
                result = self.server.store.create_project(payload.get('name'))
            elif self.path == '/api/project-rename':
                result = self.server.store.rename_project(payload.get('id'), payload.get('name'))
            elif self.path == '/api/image-sets':
                result = self.server.store.register_set(payload.get('project_id', 'default'),
                                                       payload.get('path', ''), payload.get('input_type', 'images'),
                                                       payload.get('name'))
            elif self.path == '/api/quality-analyze':
                result = self.server.store.analyze_images(payload.get('image_set_id'), payload.get('thresholds'))
            elif self.path == '/api/image-selection':
                result = self.server.store.save_selection(payload.get('image_set_id'), payload.get('names'), payload.get('fingerprint'))
            elif self.path == "/api/start":
                result = self.server.jobs.start(payload)
            elif self.path == "/api/cancel":
                result = self.server.jobs.cancel()
            elif self.path == "/api/report":
                available = {row["id"]: row for row in records(self.server.store)}
                selected = payload.get("runs", [])
                if not selected or any(value not in available for value in selected):
                    raise ValueError("请选择有效的任务。")
                output = ROOT / "outputs/reports" / datetime.now().strftime("comparison_%Y%m%d_%H%M%S_%f")
                evaluate.export_report([available[value]["path"] for value in selected], output)
                result = {"path": str(output.relative_to(ROOT))}
            elif self.path == "/api/open":
                available = {row["id"]: row for row in records(self.server.store)}
                row = available.get(payload.get("run"))
                if not row:
                    raise ValueError("请选择已完成任务。")
                if payload.get("viewer") and row["method"] == "3dgs":
                    from .train_3dgs import inspect_brush
                    brush = inspect_brush()[0]
                    gaussian = next(item["path"] for item in row["artifacts"] if item["kind"] == "gaussian_ply")
                    command = [brush, str(project_path(gaussian)), "--with-viewer"]
                else:
                    command = ["open" if sys.platform == "darwin" else "xdg-open", row["path"]]
                subprocess.Popen(command, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                result = {"opened": True}
            else:
                return self.respond({"error": "Not found"}, 404)
            self.respond(result)
        except (ValueError, OSError, KeyError, StopIteration, TypeError, SystemExit) as error:
            self.respond({"error": str(error) or "参数无效"}, 400)

    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description="本地三维重建与实验对比界面。")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    (ROOT / 'data').mkdir(exist_ok=True)
    with (ROOT / 'data/workbench.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(1, '项目服务已在运行，请使用已有界面，或先在原终端停止服务。\n')
        try:
            server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
        except OSError as error:
            parser.exit(1, '无法启动界面：' + str(error) + '。请检查端口占用。\n')
        try:
            store = Workspace(ROOT)
            store.import_history()
            store.recover()
            server.store = store
            server.token, server.jobs, server.uploads = secrets.token_urlsafe(32), Jobs(store), Uploads(store)
            url = "http://127.0.0.1:" + str(server.server_port)
            print("界面：" + url, flush=True)
            if not args.no_browser:
                webbrowser.open(url)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
        finally:
            if hasattr(server, 'jobs'):
                server.jobs.close()
            server.server_close()


if __name__ == "__main__":
    main()
