#!/usr/bin/env python3
"""Dependency-free loopback web UI for local reconstruction jobs."""
import argparse
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
from prepare_brush import image_size
from pointcloud import cloud_packet

import evaluate
import main as routes

ROOT = Path(__file__).resolve().parent


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
    def __init__(self):
        self.lock = threading.Lock()
        self.process = None
        self.output = None
        self.log = None
        self.cancel_requested = False

    def start(self, payload):
        with self.lock:
            if self.process and self.process.poll() is None:
                raise ValueError("已有任务运行中，请等待完成或停止它。")
            label = payload.get("name", "experiment").strip() or "experiment"
            if len(label) > 60 or any(char in label for char in '/\\\x00') or label in (".", ".."):
                raise ValueError("实验名称无效。")
            output = ROOT / "outputs" / (label + "_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
            argv = job_arguments(payload, output)
            logs = ROOT / "outputs/.ui_logs"
            logs.mkdir(parents=True, exist_ok=True)
            log = logs / (output.name + ".log")
            with log.open("w", encoding="utf-8") as stream:
                self.process = subprocess.Popen([sys.executable, "-u", str(ROOT / "main.py")] + argv,
                                                cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT,
                                                start_new_session=True)
            self.output, self.log, self.cancel_requested = output, log, False
            return {"output": str(output.relative_to(ROOT)), "pid": self.process.pid}

    def cancel(self):
        with self.lock:
            if self.process and self.process.poll() is None:
                os.killpg(self.process.pid, signal.SIGINT)
                self.cancel_requested = True
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
            return {"status": "running" if code is None else "completed" if code == 0 else "cancelled" if code in (130, -signal.SIGINT) or self.cancel_requested else "failed",
                    "returncode": code, "output": str(self.output.relative_to(ROOT)), "states": states,
                    "progress": {"step": int(last[1]), "total": int(last[2])} if last else None,
                    "log": log[-24000:]}


class Uploads:
    def __init__(self):
        self.sessions = {}
        self.lock = threading.Lock()

    def create(self):
        key = secrets.token_hex(12)
        path = ROOT / 'data/uploads' / key
        path.mkdir(parents=True)
        self.sessions[key] = {'path': path, 'files': [], 'bytes': 0}
        return {'id': key, 'path': str(path.relative_to(ROOT))}

    def receive(self, key, name, stream, size):
        with self.lock:
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
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            session['files'].append(name)
            session['bytes'] += size
            return {'files': len(session['files']), 'path': str(session['path'].relative_to(ROOT))}


def records():
    rows = []
    for folder in evaluate.discover_runs(ROOT / "outputs"):
        try:
            row = evaluate.collect_run(folder)
            row["id"] = str(folder.relative_to(ROOT))
            rows.append(row)
        except (ValueError, OSError, KeyError):
            continue
    return rows


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
            if url.path == "/":
                data = (ROOT / "web/index.html").read_text().replace("__TOKEN__", self.server.token)
                return self.respond(data.encode(), content_type="text/html; charset=utf-8")
            if url.path == "/api/job":
                return self.respond(self.server.jobs.snapshot())
            if url.path == "/api/runs":
                return self.respond(records())
            if url.path == '/viewer.js':
                return self.respond((ROOT / 'web/viewer.js').read_bytes(), content_type='text/javascript; charset=utf-8')
            if url.path == '/api/cloud':
                query = parse_qs(url.query)
                row = next((row for row in records() if row['id'] == query.get('run', [''])[0]), None)
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
            if not 0 < size <= 65536:
                raise ValueError("请求大小无效。")
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError("请求需要 JSON 对象。")
            if self.path == '/api/upload-new':
                result = self.server.uploads.create()
            elif self.path == "/api/start":
                result = self.server.jobs.start(payload)
            elif self.path == "/api/cancel":
                result = self.server.jobs.cancel()
            elif self.path == "/api/report":
                available = {row["id"]: row for row in records()}
                selected = payload.get("runs", [])
                if not selected or any(value not in available for value in selected):
                    raise ValueError("请选择有效的任务。")
                output = ROOT / "outputs/reports" / datetime.now().strftime("comparison_%Y%m%d_%H%M%S_%f")
                evaluate.export_report([available[value]["path"] for value in selected], output)
                result = {"path": str(output.relative_to(ROOT))}
            elif self.path == "/api/open":
                available = {row["id"]: row for row in records()}
                row = available.get(payload.get("run"))
                if not row:
                    raise ValueError("请选择已完成任务。")
                if payload.get("viewer") and row["method"] == "3dgs":
                    from train_3dgs import inspect_brush
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
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.token, server.jobs, server.uploads = secrets.token_urlsafe(32), Jobs(), Uploads()
    url = "http://127.0.0.1:" + str(server.server_port)
    print("界面：" + url, flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.jobs.cancel()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
