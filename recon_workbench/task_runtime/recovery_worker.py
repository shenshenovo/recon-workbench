"""Execution-time guard for queued recovery jobs."""
import argparse
from pathlib import Path
import subprocess
import sys

from .repository import TaskRepository, parameters_fingerprint
from .validators import (execution_fingerprint, validate_input_snapshot,
                         validate_mvs_output, validate_sfm_output, validate_prepared_dataset)
from ..workspace import Workspace


def verify(workspace, task_id, attempt_id):
    repository = TaskRepository(workspace)
    task = repository.get(task_id)
    attempt = repository.attempt(attempt_id)
    plan = attempt.get('recovery_plan') or {}
    if task.get('current_attempt_id') != attempt_id or task.get('status') not in ('starting', 'running'):
        raise ValueError('恢复尝试身份或状态已变化。')
    if parameters_fingerprint(task.get('parameters')) != plan.get('parameters_fingerprint'):
        raise ValueError('任务参数在恢复入队后发生变化。')
    if (task.get('input_fingerprint') != plan.get('input_fingerprint') or
            task.get('request_fingerprint') != plan.get('request_fingerprint')):
        raise ValueError('任务输入或请求身份在恢复入队后发生变化。')
    input_check = validate_input_snapshot(workspace, task)
    if not input_check['valid']:
        raise ValueError(input_check['reason'])
    runtime_fingerprint = execution_fingerprint(workspace.root, task.get('method'), task.get('parameters'))
    if (runtime_fingerprint != plan.get('execution_fingerprint') or
            runtime_fingerprint != attempt.get('execution_fingerprint')):
        raise ValueError('执行代码或外部工具版本已变化，请重新预检。')
    for item in plan.get('reuse', []):
        if item.get('stage') == 'sfm':
            checked = validate_sfm_output(workspace.root / item['source_path'])
        elif item.get('stage') == '3dgs.prepare':
            checked = validate_prepared_dataset(workspace.root / item['source_path'])
        elif item.get('stage', '').startswith('mvs.'):
            stage = item['stage'].split('.', 1)[1]
            checked = validate_mvs_output(workspace.root / item['source_path'], stage)
        else:
            raise ValueError('恢复计划含有未知复用阶段。')
        if not checked.get('valid') or checked.get('manifest_fingerprint') != item.get('manifest_fingerprint'):
            raise ValueError(checked.get('reason') or '复用产物指纹变化。')
    command = attempt['command']
    try:
        marker = command.index('--')
    except ValueError:
        raise ValueError('数据库记录的恢复命令格式无效。') from None
    return command[marker + 1:]


def main(argv=None):
    parser = argparse.ArgumentParser(description='启动前复核输入与复用产物。')
    parser.add_argument('--workspace', required=True, type=Path)
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--attempt-id', required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('缺少执行命令。')
    try:
        workspace_root = args.workspace.expanduser().resolve(strict=True)
        workspace = Workspace(workspace_root)
        expected = TaskRepository(workspace).attempt(args.attempt_id)['command']
        stored = verify(workspace, args.task_id, args.attempt_id)
        try:
            marker = expected.index('--')
        except ValueError:
            raise ValueError('数据库记录的恢复命令格式无效。') from None
        if command != expected[marker + 1:] or command != stored:
            raise ValueError('恢复命令与数据库记录不匹配。')
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL)
        return process.wait()
    except (ValueError, OSError, KeyError) as error:
        print('恢复启动前校验失败：' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
