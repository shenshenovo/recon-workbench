"""单执行槽后台派发；HTTP 查询不承担状态更新。"""
import logging
from pathlib import Path
import subprocess
import threading
import sqlite3

from . import processes
from .repository import TaskRepository
from .. import evaluate

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, workspace, *, popen=subprocess.Popen, cancel_timeouts=(10., 5., 5.)):
        self.workspace = workspace
        self.repository = TaskRepository(workspace)
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
        self._started = {}
        self._start_errors = {}
        self._reconcile()

    def _block(self, error):
        self.blocked_reason = 'reconciliation_required'
        self.blocked_detail = str(error)
        logger.error('任务派发已阻塞：%s', error)

    def _validate_success(self, task):
        folder = self.workspace.root / task['output']
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
            # 仅在整个组退出且算法已发布有效结果时补全 DB；取消优先。
            if task['status'] in ('starting', 'running', 'cancelling'):
                try:
                    self._validate_success(task)
                    self.repository.finish(task['id'], attempt['id'], 'completed', 0)
                    continue
                except (ValueError, OSError, KeyError):
                    pass
            self.repository.reconcile_attempt(attempt, True, '服务中断，旧执行未重新接管；已确认无残留计算，需显式重新提交。')

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.thread = threading.Thread(target=self._dispatch, name='task-runtime-dispatcher', daemon=True)
            self.thread.start()

    def notify(self):
        self.wake.set()

    def wait_started(self, task_id, timeout=.5):
        # 旧测试/调用方可等待本次尝试的 monitor；提交本身仍是持久化队列。
        event = self._started.setdefault(task_id, threading.Event())
        event.wait(timeout)
        return self._start_errors.pop(task_id, None)

    def _dispatch(self):
        try:
            while not self.stop_event.is_set():
                if self.blocked_reason:
                    self.wake.wait(.2)
                    self.wake.clear()
                    continue
                with self.lock:
                    if self.stop_event.is_set():
                        return
                    try:
                        task = self.repository.claim_next()
                    except sqlite3.OperationalError:
                        # Ephemeral test workspaces may be removed while the
                        # daemon is idle; a production workspace remains and
                        # is retried on the next wake-up.
                        if not Path(self.repository.path).exists():
                            return
                        raise
                    if task:
                        self.task_id = task['id']
                        self.worker = threading.Thread(target=self._execute, args=(task,), name='task-runtime-attempt', daemon=True)
                        self.worker.start()
                if task:
                    self.worker.join()
                    with self.lock:
                        self.task_id = None
                        self.owner = None
                else:
                    self.wake.wait(.2)
                    self.wake.clear()
        except Exception as error:
            self._block(error)
            logger.exception('后台派发异常，保留未结束 attempt 供下次核查')

    def _execute(self, task):
        attempt_id = task['current_attempt_id']
        owner = None
        cleanup_confirmed = True
        status, code, error = 'failed', None, None
        started = self._started.setdefault(task['id'], threading.Event())
        try:
            log = self.workspace.root / task['log']
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open('w', encoding='utf-8') as stream:
                owner = processes.launch(cwd=self.workspace.root, stdout=stream, popen=self.popen)
            with self.lock:
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
            try:
                self.repository.finish(task['id'], attempt_id, status, code, error, cleanup_confirmed)
            except Exception as persistence_error:
                self._block(persistence_error)
                logger.exception('保存最终状态失败，保留 attempt 供核查')
            started.set()
            self._started.pop(task['id'], None)

    def cancel(self, task_id=None):
        result = self.repository.request_cancel(task_id)
        self.notify()
        return result

    def snapshot(self, project_id=None):
        result = self.repository.queue(project_id, self.blocked_reason)
        result['blocked_detail'] = self.blocked_detail
        return result

    def close(self):
        with self.lock:
            self.stop_event.set()
            if self.task_id:
                self.repository.request_cancel(self.task_id)
        self.notify()
        if self.thread:
            self.thread.join(timeout=sum(self.cancel_timeouts) * 2 + 5)
            if self.thread.is_alive():
                self._block('服务停止时后台执行尚未退出，禁止再次派发。')
                raise processes.ProcessCleanupError(self.blocked_detail)
