"""Stage-aware read-only preflight and explicit recovery enqueue."""
import json
import hashlib
from pathlib import Path
import sys
import uuid

from .repository import RuntimeConflict, TaskNotFound, parameters_fingerprint
from .snapshots import digest
from .snapshots import safe_path
from .validators import (stage_state, validate_input_snapshot,
                         validate_mvs_output,
                         validate_sfm_output, execution_fingerprint, validate_output_manifest,
                         validate_prepared_dataset)


class RecoveryManager:
    def __init__(self, workspace, repository, arguments):
        self.workspace = workspace
        self.repository = repository
        self.arguments = arguments

    def _source_attempt(self, task):
        attempt_id = task.get('current_attempt_id')
        if attempt_id:
            try:
                return self.repository.attempt(attempt_id)
            except TaskNotFound:
                pass
        return {'id': None, 'output_path': task.get('output')}

    def _output(self, task, attempt):
        relative = attempt.get('output_path') or task.get('output')
        return safe_path(self.workspace.root, relative)

    @staticmethod
    def _failed_stage(stages):
        return next((stage.get('name') for stage in reversed(stages)
                     if stage.get('status') in ('failed', 'cancelled', 'running')), None)

    def inspect(self, task_id):
        task = self.repository.get(task_id)
        if task['status'] not in ('failed', 'interrupted'):
            error = RuntimeConflict('仅失败或中断的任务可以恢复。')
            error.code = 'task_not_resumable'
            raise error
        source = self._source_attempt(task)
        output = self._output(task, source)
        input_check = validate_input_snapshot(self.workspace, task)
        parameters = task.get('parameters') or {}
        current_execution_fingerprint = execution_fingerprint(
            self.workspace.root, task.get('method'), parameters)
        compatible = (source.get('request_fingerprint') == task.get('request_fingerprint') and
                      source.get('input_fingerprint') == task.get('input_fingerprint') and
                      source.get('execution_fingerprint') == current_execution_fingerprint and
                      source.get('parameters_fingerprint') == parameters_fingerprint(parameters))
        status, outer_stages = stage_state(output)
        failed_stage = self._failed_stage(outer_stages)
        completed = [stage.get('name') for stage in outer_stages if stage.get('status') == 'completed']
        params = parameters
        method = task.get('method')
        reuse, rerun, mvs_resume = [], [], None
        blocked = []

        if source.get('id') and not source.get('cleanup_confirmed'):
            blocked.append('原执行尝试尚未确认清理完成，不能恢复。')

        if not input_check['valid']:
            blocked.append(input_check['reason'])
        if source.get('parameters_fingerprint') and source['parameters_fingerprint'] != parameters_fingerprint(parameters):
            blocked.append('任务参数与原执行尝试不一致，请创建新任务。')
        warnings = []
        if source.get('id') and not compatible:
            warnings.append('原 attempt 的输入、参数或执行版本指纹不完整/不一致，所有阶段保守重做。')
        prior_reuse = (source.get('recovery_plan') or {}).get('reuse', [])

        def reusable(stage, path, validator):
            if not compatible:
                return None
            candidates = [(path, source.get('id'))]
            candidates += [(self.workspace.root / item['source_path'], item.get('source_attempt_id'))
                           for item in prior_reuse if item.get('stage') == stage]
            for candidate, owner in candidates:
                try:
                    candidate = safe_path(self.workspace.root, candidate)
                    checked = validator(candidate)
                except (ValueError, OSError):
                    continue
                if checked['valid']:
                    return {'stage': stage, 'source_attempt_id': owner,
                            'source_path': candidate.relative_to(self.workspace.root).as_posix(),
                            'manifest_fingerprint': checked['manifest_fingerprint']}
            return None

        if method in ('mvs', '3dgs'):
            sfm = reusable('sfm', output / 'sfm', validate_sfm_output)
            if sfm:
                reuse.append(sfm)
            elif params.get('input_type') == 'images':
                rerun.append('sfm')
        if method == 'mvs':
            requested = params.get('mvs_stage', 'textured')
            mvs_output = output / 'mvs'
            mvs_status, _ = stage_state(mvs_output)
            dense_result = mvs_status.get('dense_result')
            completed_mvs = dense_result.get('completed_stage') if isinstance(dense_result, dict) else None
            dependency_valid = (any(item['stage'] == 'sfm' for item in reuse) or
                                params.get('input_type') == 'sfm-output')
            # A failed publication at the requested boundary is conservatively
            # rerun; no result is invented from a partial summary.
            if compatible and dependency_valid and completed_mvs in ('dense', 'mesh'):
                next_stage = 'mesh' if completed_mvs == 'dense' else 'textured'
                if ('dense', 'mesh', 'textured').index(requested) > ('dense', 'mesh', 'textured').index(completed_mvs):
                    item = reusable('mvs.' + completed_mvs, mvs_output,
                                    lambda path: validate_mvs_output(path, completed_mvs))
                    if item:
                        reuse.append(item)
                        mvs_resume = next_stage
                        rerun.extend(['mvs.mesh', 'mvs.textured'] if next_stage == 'mesh' and requested == 'textured'
                                     else ['mvs.' + next_stage])
            if not mvs_resume:
                rerun.append('mvs')
        elif method == 'sfm':
            # COLMAP feature extraction, matching and mapping is one unit.
            rerun.append('sfm')
        elif method == '3dgs':
            dependency_valid = (any(item['stage'] == 'sfm' for item in reuse) or
                                params.get('input_type') in ('sfm-output', 'dataset'))
            dataset = reusable('3dgs.prepare', output / '3dgs/dataset', validate_prepared_dataset) if dependency_valid else None
            if dataset:
                reuse.append(dataset)
            elif params.get('input_type') != 'dataset':
                rerun.append('3dgs.prepare')
            rerun.append('3dgs.train')
        elif method == 'fast3r':
            rerun.append('fast3r')
        else:
            blocked.append('该任务路线没有经过验证的恢复适配器。')

        if not rerun and not reuse:
            rerun.append(method or 'task')
        plan = {
            'format_version': 1,
            'task_id': task_id,
            'source_attempt_id': source.get('id'),
            'source_output': str(output.relative_to(self.workspace.root)) if output.is_relative_to(self.workspace.root) else None,
            'input_fingerprint': task.get('input_fingerprint'),
            'request_fingerprint': task.get('request_fingerprint'),
            'execution_fingerprint': current_execution_fingerprint,
            'parameters_fingerprint': parameters_fingerprint(parameters),
            'dependencies': {'sfm': [], 'mvs.dense': ['sfm'], 'mvs.mesh': ['mvs.dense'],
                             'mvs.textured': ['mvs.mesh'], '3dgs.prepare': ['sfm'],
                             '3dgs.train': ['3dgs.prepare'], 'fast3r': []},
            'failed_stage': failed_stage,
            'completed_stages': completed,
            'reuse': reuse,
            'rerun': rerun,
            'mvs_resume_stage': mvs_resume,
            'mvs_manifest_fingerprint': next((item['manifest_fingerprint'] for item in reuse
                                               if item['stage'].startswith('mvs.')), None),
            'blocked_reasons': blocked,
            'warnings': warnings,
        }
        plan['plan_fingerprint'] = digest(plan)
        return {'task_id': task_id, 'can_resume': not blocked,
                'plan_fingerprint': plan['plan_fingerprint'], 'reuse': reuse,
                'rerun': rerun, 'blocked_reasons': blocked,
                'warnings': warnings,
                'source_attempt_id': source.get('id'), '_plan': plan}

    def resume(self, task_id, plan_fingerprint):
        task = self.repository.get(task_id)
        current_id = task.get('current_attempt_id')
        if task['status'] in ('queued', 'starting', 'running', 'cancelling') and current_id:
            current = self.repository.attempt(current_id)
            if current.get('resume_fingerprint') == plan_fingerprint:
                return {'task_id': task_id, 'attempt_id': current_id, 'status': task['status'],
                        'queue_position': None, 'created': False}
            error = RuntimeConflict('该任务已有不同的恢复尝试。')
            error.code = 'task_already_active'
            raise error
        check = self.inspect(task_id)
        if check['plan_fingerprint'] != plan_fingerprint:
            error = RuntimeConflict('恢复预检指纹已变化，请重新检查。')
            error.code = 'resume_plan_changed'
            raise error
        if not check['can_resume']:
            error = RuntimeConflict('恢复预检未通过：' + '；'.join(check['blocked_reasons']))
            error.code = 'resume_blocked'
            raise error
        plan = check['_plan']
        attempt_id = uuid.uuid4().hex
        base = self.workspace.root / (task.get('output_root') or task['output'])
        output = base / 'attempts' / attempt_id
        relative_output = self.workspace.relative(output)
        log = self.workspace.root / 'outputs/.ui_logs' / (task_id + '_' + attempt_id + '.log')
        relative_log = self.workspace.relative(log)
        parameters = dict(task.get('parameters') or {})
        parameters['output'] = str(output)
        if plan.get('mvs_resume_stage'):
            parameters['_runtime_resume'] = {
                'mvs_resume_from': str(self.workspace.root / next(
                    item['source_path'] for item in plan['reuse'] if item['stage'].startswith('mvs.'))),
                'mvs_resume_stage': plan['mvs_resume_stage'],
                'mvs_manifest_fingerprint': plan['mvs_manifest_fingerprint'],
            }
        # An earlier successful SFM output remains an immutable, verified
        # read-only input to the new attempt; the attempt's own outputs remain isolated.
        if any(item.get('stage') == 'sfm' for item in plan['reuse']):
            parameters['_runtime_resume'] = parameters.get('_runtime_resume', {})
            sfm_source = next(item['source_path'] for item in plan['reuse'] if item['stage'] == 'sfm')
            parameters['_runtime_resume']['sfm_resume_from'] = str(self.workspace.root / sfm_source)
            parameters['input_type'] = 'sfm-output'
            parameters['input'] = parameters['_runtime_resume']['sfm_resume_from']
        prepared = next((item for item in plan['reuse'] if item['stage'] == '3dgs.prepare'), None)
        if prepared:
            parameters['input_type'] = 'dataset'
            parameters['input'] = str(self.workspace.root / prepared['source_path'])
        algorithm_command = [sys.executable, '-u', str(self.workspace.root / 'main.py')]
        algorithm_command += self.arguments(parameters, output)
        command = [sys.executable, '-m', 'recon_workbench.task_runtime.recovery_worker',
                   '--workspace', str(self.workspace.root), '--task-id', task_id,
                   '--attempt-id', attempt_id, '--'] + algorithm_command
        result = self.repository.enqueue_recovery(task_id, plan, command, relative_output, relative_log,
                                                  attempt_id=attempt_id)
        return {'task_id': task_id, 'attempt_id': result['attempt_id'],
                'status': self.repository.get(task_id)['status'],
                'queue_position': self.repository.response(task_id)['queue_position'],
                'created': result['created']}


def copy_verified_mvs_artifacts(source, destination, manifest_fingerprint):
    """Copy only artifacts needed to resume mesh or texture after a fresh check."""
    source = Path(source).resolve(strict=True)
    checked = validate_output_manifest(source)
    if not checked['valid'] or checked['manifest'].get('manifest_fingerprint') != manifest_fingerprint:
        raise ValueError(checked.get('reason') or 'MVS 复用产物指纹变化。')
    state = json.loads((source / 'status.json').read_text(encoding='utf-8'))
    completed = (state.get('dense_result') or {}).get('completed_stage')
    names = ['scene.mvs', 'dense.mvs', 'dense.ply', 'dense_colmap.ply']
    if completed in ('mesh', 'textured'):
        names.extend(['mesh.mvs', 'mesh.ply'])
    records = {item['path']: item for item in checked['manifest']['files']}
    relatives = ['dense/' + name for name in names]
    relatives += [relative for relative in records if relative.startswith('undistorted/')]
    for relative in relatives:
        name = relative
        target_root = Path(destination)
        record = records.get(relative)
        original = source / relative
        if record is None or original.is_symlink() or not original.is_file():
            raise ValueError('MVS 复用产物缺失：' + name)
        target = target_root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        hasher = hashlib.sha256()
        with original.open('rb') as src, target.open('xb') as dst:
            while True:
                block = src.read(1024 * 1024)
                if not block:
                    break
                dst.write(block)
                hasher.update(block)
        if target.stat().st_size != record['bytes'] or hasher.hexdigest() != record['sha256']:
            target.unlink(missing_ok=True)
            raise ValueError('复制中的 MVS 产物校验失败：' + name)
