"""现有 Jobs 的适配层：校验与快照完成后发布到持久化队列。"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
import uuid

from .repository import TaskRepository, TaskNotFound
from .scheduler import Scheduler
from . import snapshots


class RuntimeJobs:
    def __init__(self, store, *, arguments, popen):
        if store is None:
            raise ValueError('持久化任务队列需要 Workspace。')
        self.store = store
        self.arguments = arguments
        self.scheduler = Scheduler(store, popen=popen)
        self.repository = self.scheduler.repository
        self.submit_lock = threading.RLock()
        self.scheduler.start()

    @property
    def worker(self):
        return self.scheduler.worker

    @property
    def process(self):
        return self.scheduler.owner.process if self.scheduler.owner else None

    def _platform_check(self, method):
        if os.name != 'posix':
            raise ValueError('PR1 仅支持 POSIX 本地子进程，不支持 Windows 原生执行。')

    def _business(self, payload):
        """以解析后的默认值与类型生成业务指纹，不包含输出名或时间。"""
        from .. import pipeline
        args = pipeline.get_parser().parse_args(self.arguments(payload, self.store.root / 'outputs/.validation'))
        values = vars(args).copy()
        for key in ('output', 'images', 'dataset', 'sfm_output'):
            values.pop(key, None)
        return {key: str(value) if isinstance(value, Path) else value for key, value in values.items()}

    def start(self, payload):
        # 同一服务内串行准备避免重复快照；SQLite 唯一约束仍负责跨连接去重。
        with self.submit_lock:
            return self._submit(dict(payload))

    def _submit(self, payload):
        root = self.store.root
        key = payload.get('idempotency_key')
        if key is not None and (not isinstance(key, str) or not 1 <= len(key) <= 128 or '\x00' in key):
            raise ValueError('幂等键需要 1～128 个字符。')
        name = payload.get('name', 'experiment')
        if not isinstance(name, str):
            raise ValueError('实验名称无效。')
        name = name.strip() or 'experiment'
        if len(name) > 60 or any(char in name for char in '/\\\x00') or name in ('.', '..'):
            raise ValueError('实验名称无效。')
        project_id = payload.get('project_id', 'default')
        self.store.project(project_id)
        image_set = None
        if payload.get('image_set_id'):
            image_set = self.store.image_set(payload['image_set_id'])
            if image_set['project_id'] != project_id:
                raise ValueError('图片集不属于所选项目。')
            payload.update(input=image_set['path'], input_type=image_set['input_type'])
        business = self._business(payload)
        if image_set is None:
            image_set = self.store.register_set(project_id, payload['input'], payload.get('input_type', 'dataset'))
        # 原键重放使用第一次固定的输入身份，即使原件后来移动/筛选改变。
        with self.repository.connect() as db:
            existing = db.execute('SELECT id,parameters FROM tasks WHERE project_id=? AND idempotency_key=?', (project_id, key)).fetchone() if key else None
        if existing:
            old = json.loads(existing['parameters'])
            identity = old['submission_identity']
            fingerprint = snapshots.digest({'project_id': project_id, 'image_set_id': image_set['id'],
                                             'name': name, 'business': business, 'input': identity})
            return self.repository.response(self.repository.by_key(project_id, key, fingerprint)['id'],
                                           self.scheduler.blocked_reason, self.scheduler.resources)
        self._platform_check(business['method'])
        resource_request = self.scheduler.resources.request_for(business['method'], business)
        description = snapshots.describe_input(self.store, image_set)
        if business['method'] == 'fast3r' and len(description['names']) > business['fast3r_max_views']:
            raise ValueError('Fast3R 选中照片超过当前数量上限，请减少照片或调整上限。')
        fingerprint = snapshots.digest({'project_id': project_id, 'image_set_id': image_set['id'],
                                         'name': name, 'business': business, 'input': description['identity']})
        task_id = uuid.uuid4().hex
        output = root / 'outputs' / (name + '_' + task_id)
        log = root / 'outputs/.ui_logs' / (output.name + '.log')
        parameters = dict(payload, project_id=project_id, image_set_id=image_set['id'],
                          submission_identity=description['identity'], resource_request=resource_request)
        task, created = self.repository.prepare(project_id=project_id, image_set_id=image_set['id'], name=name,
            method=business['method'], output=self.store.relative(output), log=self.store.relative(log), parameters=parameters,
            command=[], request_fingerprint=fingerprint, idempotency_key=key, task_id=task_id,
            resource_request=resource_request)
        if not created:
            return self.repository.response(task['id'], self.scheduler.blocked_reason, self.scheduler.resources)
        try:
            parameters = snapshots.prepare(self.store, task, description, parameters)
            command = [__import__('sys').executable, '-u', str(root / 'main.py')] + self.arguments(parameters, output)
            if not self.repository.publish(task_id, parameters, command, description['fingerprint'], resource_request):
                return self.repository.response(task_id, self.scheduler.blocked_reason, self.scheduler.resources)
        except Exception as error:
            self.repository.fail_preparing(task_id, error)
            raise
        self.scheduler.notify()
        start_error = self.scheduler.wait_started(task_id)
        if start_error is not None:
            raise OSError(str(start_error))
        return self.repository.response(task_id, self.scheduler.blocked_reason, self.scheduler.resources)

    def cancel(self, task_id=None):
        if task_id is not None and not isinstance(task_id, str):
            raise ValueError('task_id 必须是字符串。')
        return self.scheduler.cancel(task_id)

    def close(self):
        self.scheduler.close()

    def snapshot_queue(self, project_id=None):
        return self.scheduler.snapshot(project_id)

    def resources_snapshot(self):
        return self.scheduler.resource_snapshot()

    def resume_queue(self):
        return self.scheduler.resume_dispatch()

    def snapshot(self, task_id=None):
        from ..server import task_detail
        if task_id is None:
            queue = self.snapshot_queue()
            tasks = queue['running'] or queue['queued']
            if tasks:
                task_id = tasks[0]['id']
            else:
                tasks = self.store.tasks()
                if not tasks:
                    return {'status': 'idle', 'log': '', 'states': [], 'progress': None}
                task_id = tasks[0]['id']
        task = task_detail(self.store, task_id)
        import re
        progress = list(re.finditer(r'(\d+)\s*/\s*(\d+)\s+Steps', task['log_text']))
        last = progress[-1] if progress else None
        return {**self.repository.response(task_id, self.scheduler.blocked_reason, self.scheduler.resources), 'returncode': task['returncode'],
                'log': task['log_text'], 'states': task['states'],
                'progress': {'step': int(last[1]), 'total': int(last[2])} if last else None}
