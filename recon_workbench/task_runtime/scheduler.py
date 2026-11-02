"""受资源预算约束的持久化任务派发器。"""
import logging
from pathlib import Path
import subprocess
import threading
import sqlite3

from . import processes
from .repository import RuntimeConflict, TaskRepository
from .resources import ResourceManager
from .validators import capture_output_manifests
from .. import evaluate

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, workspace, *, popen=subprocess.Popen, cancel_timeouts=(10., 5., 5.),
                 resource_config=None, resource_probe=None):
        self.workspace = workspace
        self.repository = TaskRepository(workspace)
        self.resources = ResourceManager(workspace, config=resource_config, probe=resource_probe)
        self.popen = popen
        self.cancel_timeouts = cancel_timeouts
        self.blocked_reason = None
        self.blocked_detail = None
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.lock = threading.RLock()
        self.thread = None
        self.worker = None
        self.owner = None
        self.task_id = None
        self.workers = {}
        self.owners = {}
        self._started = {}
        self._start_errors = {}
        self._reconcile()
        try:
            self.resources.validate_ledger(self.repository)
        except RuntimeConflict as error:
            self._block(error)
        if self.repository.persistent_dispatch_pause() and not self.blocked_reason:
            self.blocked_reason = 'restored_workspace_confirmation'
            self.blocked_detail = '工作区来自离线备份；请核对环境后调用 /api/queue-resume。'

    def _block(self, error):
        self.blocked_reason = 'reconciliation_required'
        self.blocked_detail = str(error)
        logger.error('任务派发已阻塞：%s', error)

    def _validate_success(self, task):
        folder = self.workspace.root / task.get('attempt_output', task['output'])
        state = evaluate.read_json(folder / 'status.json')
        summary = evaluate.read_json(folder / 'summary.json')
        if state.get('status') != 'completed' or not summary:
            raise ValueError('进程退出但缺少已完成的 status.json / summary.json，请查看日志。')
        evaluate.collect_run(folder)

    def _reconcile(self):
        for task in self.workspace.tasks():
            if task['status'] == 'preparing':
                self.repository.fail_preparing(task['id'], '服务在输入准备期间中断；快照未发布为可执行任务，请新建任务。')
        for attempt in self.repository.unresolved():
            try:
                processes.reconcile(attempt)
            except (OSError, processes.ProcessCleanupError) as error:
                self.repository.reconcile_attempt(attempt, False, str(error))
                self._block(error)
                continue
            task = self.repository.get(attempt['task_id'])
            task['attempt_output'] = attempt.get('output_path') or task['output']
            # 仅在整个组退出且算法已发布有效结果时补全 DB；取消优先。
            if task['status'] in ('starting', 'running', 'cancelling'):
                try:
                    self._validate_success(task)
                    self.repository.finish(task['id'], attempt['id'], 'completed', 0)
                    continue
                except (ValueError, OSError, KeyError):
                    pass
            self.repository.reconcile_attempt(
                attempt, True,
                '服务中断，旧执行未重新接管；已确认无残留计算，需显式重新提交。'
            )
            try:
                output = attempt.get('output_path') or task.get('output')
                capture_output_manifests(self.workspace.root, self.workspace.root / output)
            except (OSError, ValueError):
                logger.exception('生成中断 attempt 的恢复产物清单失败')

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.thread = threading.Thread(target=self._dispatch, name='task-runtime-dispatcher', daemon=True)
            self.thread.start()

    def notify(self):
        self.wake.set()

    def wait_started(self, task_id, timeout=.5):
        # 提交本身仍是持久化队列；此事件只提供旧调用方的短暂启动错误反馈。
        event = self._started.setdefault(task_id, threading.Event())
        event.wait(timeout)
        return self._start_errors.pop(task_id, None)

    def _prune_workers(self):
        with self.lock:
            for task_id, worker in list(self.workers.items()):
                if not worker.is_alive():
                    self.workers.pop(task_id, None)
                    self.owners.pop(task_id, None)
            if self.workers:
                self.task_id = next(iter(self.workers))
                self.worker = self.workers[self.task_id]
                self.owner = self.owners.get(self.task_id)
            else:
                self.task_id = None
                self.owner = None

    def _dispatch(self):
        try:
            while not self.stop_event.is_set():
                started_any = False
                with self.lock:
                    self._prune_workers()
                    if not self.blocked_reason:
                        capacity = self.resources.config.max_running_tasks - len(self.workers)
                        for _ in range(max(0, capacity)):
                            if self.stop_event.is_set():
                                break
                            try:
                                task = self.repository.claim_next(self.resources)
                            except sqlite3.OperationalError:
                                if not Path(self.repository.path).exists():
                                    return
                                raise
                            if task is None:
                                break
                            worker = threading.Thread(
                                target=self._execute, args=(task,),
                                name='task-runtime-attempt-' + task['id'][:8], daemon=True,
                            )
                            self.workers[task['id']] = worker
                            self.worker = worker
                            self.task_id = task['id']
                            worker.start()
                            started_any = True
                if not started_any:
                    self.wake.wait(.2)
                    self.wake.clear()
        except Exception as error:
            self._block(error)
            logger.exception('后台派发异常，保留未结束 attempt 供下次核查')

    def _worker_done(self, task_id):
        with self.lock:
            self.workers.pop(task_id, None)
            self.owners.pop(task_id, None)
            self._prune_workers()
        self.notify()

    def _execute(self, task):
        attempt_id = task['current_attempt_id']
        attempt = self.repository.attempt(attempt_id)
        owner = None
        cleanup_confirmed = True
        status, code, error = 'failed', None, None
        started = self._started.setdefault(task['id'], threading.Event())
        try:
            log = self.workspace.root / (attempt.get('log_path') or task['log'])
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open('w', encoding='utf-8') as stream:
                environment = self.resources.execution_env(task)
                environment.update(RECON_TASK_ID=task['id'], RECON_ATTEMPT_ID=attempt_id,
                                   RECON_REQUEST_FINGERPRINT=task.get('request_fingerprint') or '',
                                   RECON_INPUT_FINGERPRINT=task.get('input_fingerprint') or '')
                owner = processes.launch(
                    cwd=self.workspace.root,
                    stdout=stream,
                    popen=self.popen,
                    env=environment,
                )
            with self.lock:
                self.owners[task['id']] = owner
                self.owner = owner
            allowed = self.repository.set_process(task['id'], attempt_id, owner.pid, owner.pgid, owner.identity)
            if allowed and not self.stop_event.is_set():
                owner.release(task['command'])
            else:
                owner.process.stdin.close()  # 取消先于握手：EOF，不启动计算。
            started.set()
            while owner.poll() is None:
                current = self.repository.get(task['id'])
                if current['status'] == 'cancelling' or self.stop_event.is_set():
                    if self.stop_event.is_set():
                        self.repository.request_cancel(task['id'])
                    owner.cleanup(self.cancel_timeouts)
                    break
                try:
                    owner.wait(timeout=.1)
                except subprocess.TimeoutExpired:
                    pass
            code = owner.wait(timeout=1)
            # 父进程退出（包括异常退出）后也清理遗留子孙。
            owner.cleanup(self.cancel_timeouts)
            if self.repository.get(task['id'])['status'] == 'cancelling':
                status = 'cancelled'
            elif code == 0:
                self._validate_success(task)
                status = 'completed'
            else:
                error = '任务执行失败，请查看日志。'
        except Exception as exception:
            error = str(exception)
            if owner is None:
                self._start_errors[task['id']] = exception
            if owner:
                try:
                    if owner.process.stdin and not owner.process.stdin.closed:
                        owner.process.stdin.close()
                    owner.cleanup(self.cancel_timeouts)
                except Exception as cleanup_error:
                    cleanup_confirmed = False
                    error += '；清理失败：' + str(cleanup_error)
                    self._block(error)
            if not cleanup_confirmed:
                status = 'interrupted'
        finally:
            if cleanup_confirmed and status in ('failed', 'interrupted'):
                try:
                    capture_output_manifests(self.workspace.root,
                        self.workspace.root / task.get('attempt_output', task['output']))
                except (OSError, ValueError) as manifest_error:
                    error = (error + '；' if error else '') + '产物清单生成失败：' + str(manifest_error)
            try:
                self.repository.finish(task['id'], attempt_id, status, code, error, cleanup_confirmed)
            except Exception as persistence_error:
                self._block(persistence_error)
                logger.exception('保存最终状态失败，保留 attempt 供核查')
            started.set()
            self._started.pop(task['id'], None)
            self._worker_done(task['id'])

    def cancel(self, task_id=None):
        result = self.repository.request_cancel(task_id)
        self.notify()
        return result

    def snapshot(self, project_id=None):
        result = self.repository.queue(project_id, self.blocked_reason, self.resources)
        result['blocked_detail'] = self.blocked_detail
        return result

    def resource_snapshot(self):
        return {
            **self.resources.snapshot(self.repository),
            'dispatch_paused': bool(self.blocked_reason),
            'blocked_reason': self.blocked_reason,
            'blocked_detail': self.blocked_detail,
        }

    def resume_dispatch(self):
        with self.lock:
            self._prune_workers()
            if self.workers:
                error = RuntimeConflict('活动任务仍在运行，不能重新核查派发状态。')
                error.code = 'resource_state_conflict'
                raise error
            self.blocked_reason = None
            self.blocked_detail = None
            self._reconcile()
            try:
                self.resources.validate_ledger(self.repository)
            except RuntimeConflict as error:
                self._block(error)
            if self.blocked_reason:
                error = RuntimeConflict(self.blocked_detail or '仍有残留执行需要人工核查。')
                error.code = 'reconciliation_required'
                raise error
            self.repository.set_persistent_dispatch_pause(False)
            self.notify()
            return self.resource_snapshot()

    def close(self):
        with self.lock:
            self.stop_event.set()
            for task_id in list(self.workers):
                try:
                    self.repository.request_cancel(task_id)
                except (RuntimeConflict, OSError):
                    pass
        self.notify()
        if self.thread:
            self.thread.join(timeout=sum(self.cancel_timeouts) * 2 + 5)
        deadline = sum(self.cancel_timeouts) * 2 + 5
        for worker in list(self.workers.values()):
            worker.join(timeout=deadline)
        self._prune_workers()
        if self.workers:
            self._block('服务停止时后台执行尚未退出，禁止再次派发。')
            raise processes.ProcessCleanupError(self.blocked_detail)
