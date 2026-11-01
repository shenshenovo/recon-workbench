"""POSIX 执行会话、启动握手和经确认的进程组清理。"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field


class ProcessCleanupError(RuntimeError):
    pass


def process_identity(pid):
    """Linux 使用 boot ID + start ticks；其他 POSIX 返回未知并保守恢复。"""
    if not sys.platform.startswith('linux'):
        return None
    try:
        stat = Path(f'/proc/{pid}/stat').read_text()
        fields = stat[stat.rindex(')') + 2:].split()
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        return f'{boot}:{fields[19]}'
    except (OSError, ValueError, IndexError):
        return None


def group_alive(pgid):
    if not isinstance(pgid, int) or pgid <= 1 or pgid == os.getpgrp():
        raise ProcessCleanupError('进程组身份无效，拒绝发送信号。')
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError as error:
        raise ProcessCleanupError('没有权限核查进程组。') from error
    if sys.platform.startswith('linux'):
        # 僵尸没有执行能力；它们可能要等系统 init 回收，不能据此
        # 永久占用计算槽。读取失败一律视为未知，不能推断空闲。
        try:
            for path in Path('/proc').iterdir():
                if not path.name.isdecimal():
                    continue
                try:
                    raw = (path / 'stat').read_text()
                    fields = raw[raw.rindex(')') + 2:].split()
                    if int(fields[2]) == pgid and fields[0] != 'Z':
                        return True
                except FileNotFoundError:
                    continue
            return False
        except (OSError, ValueError, IndexError) as error:
            raise ProcessCleanupError('无法核查进程组成员。') from error
    return True


@dataclass
class OwnedProcess:
    process: subprocess.Popen
    pgid: int
    identity: str | None
    released: bool = field(default=False)

    @property
    def pid(self):
        return self.process.pid

    def release(self, command):
        """SQLite 已登记身份后才允许包装器执行计算。"""
        if self.released:
            raise RuntimeError('执行握手只能发送一次。')
        self.process.stdin.write((json.dumps(list(command)) + '\n').encode())
        self.process.stdin.flush()
        self.process.stdin.close()
        self.released = True

    def poll(self):
        return self.process.poll()

    def wait(self, timeout=None):
        return self.process.wait(timeout=timeout)

    def cleanup(self, timeouts=(10.0, 5.0, 5.0)):
        """父进程退出后仍核查整个组，按 INT/TERM/KILL 升级。"""
        for sig, timeout in zip((signal.SIGINT, signal.SIGTERM, signal.SIGKILL), timeouts):
            self.process.poll()  # 回收直接子进程，避免自身的僵尸影响核查。
            if not group_alive(self.pgid):
                self.process.wait(timeout=1)
                return
            try:
                os.killpg(self.pgid, sig)
            except ProcessLookupError:
                continue
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                self.process.poll()
                if not group_alive(self.pgid):
                    self.process.wait(timeout=1)
                    return
                time.sleep(.02)
        if group_alive(self.pgid):
            raise ProcessCleanupError('信号升级后仍无法确认进程树退出，派发已阻塞。')
        self.process.wait(timeout=1)


def launch(*, cwd, stdout, popen=subprocess.Popen):
    if os.name != 'posix':
        raise ValueError('PR1 仅支持 POSIX 本地进程组；Windows 原生进程树尚未支持。')
    env = os.environ.copy()
    env['RECON_RUNTIME_OWNED'] = '1'
    process = popen([sys.executable, '-u', str(Path(__file__).with_name('worker.py'))],
                    cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=stdout,
                    stderr=subprocess.STDOUT, start_new_session=True)
    # start_new_session 保证组号=PID，即便包装器极早退出也无需 getpgid。
    return OwnedProcess(process, process.pid, process_identity(process.pid))


def reconcile(attempt, timeouts=(1.0, 1.0, 1.0)):
    """只清理身份匹配的 Linux 会话；旧 PID 从不被当作所有权证明。"""
    pid, pgid = attempt['pid'], attempt['pgid']
    if pid is None:
        # 身份尚未提交就没有发出执行握手；服务断开后包装器收到 EOF。
        return True
    if not pgid:
        raise ProcessCleanupError('旧执行没有进程组身份，请人工核查残留进程。')
    if not group_alive(pgid):
        return True
    if not attempt['process_start'] or process_identity(pid) != attempt['process_start']:
        raise ProcessCleanupError('旧执行身份无法确认，请检查日志及进程后再恢复派发。')
    if os.getpgid(pid) != pgid or os.getsid(pid) != pid:
        raise ProcessCleanupError('旧执行会话身份变化，拒绝清理。')
    for sig, timeout in zip((signal.SIGINT, signal.SIGTERM, signal.SIGKILL), timeouts):
        # 组长一旦消失且仍有成员，不再凭已过期的 PID 发送信号。
        if not group_alive(pgid):
            return True
        if process_identity(pid) != attempt['process_start']:
            raise ProcessCleanupError('核查期间旧执行身份消失且仍有残留，请人工核查。')
        os.killpg(pgid, sig)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not group_alive(pgid):
                return True
            time.sleep(.02)
    raise ProcessCleanupError('旧执行清理未完成，请人工核查。')
