"""持久化任务运行时的 CPU/GPU 资源预算、探测和分配辅助。"""

from dataclasses import dataclass
import csv
import json
import math
import os
from pathlib import Path
import re
import subprocess
import threading
import time

from .repository import RuntimeConflict


class ResourceConflict(RuntimeConflict):
    code = 'resource_unavailable'


class ResourceConfigurationError(ResourceConflict):
    code = 'invalid_resource_configuration'


class UnsupportedGpuBackend(ResourceConflict):
    code = 'gpu_backend_unverified'


UNVERIFIED_GPU_METHODS = {'3dgs': 'brush'}




@dataclass(frozen=True)
class ResourceConfig:
    cpu_slots: int
    max_running_tasks: int
    gpu_devices: tuple
    method_backends: dict


@dataclass(frozen=True)
class DeviceObservation:
    selector: str
    name: str | None = None
    uuid: str | None = None
    memory_total_bytes: int | None = None
    memory_used_bytes: int | None = None
    utilization_percent: float | None = None


class NvidiaSmiProbe:
    """轻量级 CUDA 设备探测；监控字段失败时保持 unknown。"""

    QUERY = (
        'index,uuid,name,memory.total,memory.used,utilization.gpu'
    )

    def __call__(self):
        try:
            result = subprocess.run(
                ['nvidia-smi', f'--query-gpu={self.QUERY}', '--format=csv,noheader,nounits'],
                capture_output=True, text=True, timeout=3, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return {'status': 'unavailable', 'error': str(error), 'devices': []}
        if result.returncode != 0:
            message = (result.stderr or result.stdout or 'nvidia-smi 失败').strip()
            return {'status': 'unavailable', 'error': message, 'devices': []}
        devices = []
        try:
            rows = csv.reader(result.stdout.splitlines(), skipinitialspace=True)
            for row in rows:
                if not row or len(row) < 6:
                    continue
                selector, uuid, name, total, used, utilization = [value.strip() for value in row[:6]]
                devices.append(DeviceObservation(
                    selector=selector,
                    uuid=uuid or None,
                    name=name or None,
                    memory_total_bytes=_mib_bytes(total),
                    memory_used_bytes=_mib_bytes(used),
                    utilization_percent=_percent(utilization),
                ))
        except (TypeError, ValueError) as error:
            return {'status': 'unavailable', 'error': f'nvidia-smi 输出无效：{error}', 'devices': []}
        return {'status': 'available', 'error': None, 'devices': devices}


def _mib_bytes(value):
    if value in ('', 'N/A', '[Not Supported]'):
        return None
    number = float(value)
    return int(number * 1024 * 1024) if math.isfinite(number) and number >= 0 else None


def _percent(value):
    if value in ('', 'N/A', '[Not Supported]'):
        return None
    number = float(value)
    return number if math.isfinite(number) and 0 <= number <= 100 else None


def _positive_int(value, field):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ResourceConfigurationError(f'{field} 必须是正整数。')
    result = value
    if result < 1:
        raise ResourceConfigurationError(f'{field} 必须是正整数。')
    return result


def load_config(root, override=None):
    """读取严格校验的本地配置；未配置时保持单任务、GPU 禁用。"""
    if override is None:
        path = Path(root) / 'data/task_runtime/resources.json'
        if path.is_file():
            try:
                override = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError) as error:
                raise ResourceConfigurationError(f'资源配置无法读取：{error}') from error
        else:
            override = {}
    if not isinstance(override, dict):
        raise ResourceConfigurationError('资源配置必须是 JSON 对象。')

    host_cpu = max(1, os.cpu_count() or 1)
    cpu_slots = _positive_int(override.get('cpu_slots', host_cpu), 'cpu_slots')
    if cpu_slots > host_cpu:
        raise ResourceConfigurationError(f'cpu_slots 不能超过本机 CPU 数量 {host_cpu}。')
    max_running = _positive_int(override.get('max_running_tasks', 1), 'max_running_tasks')
    if max_running > 128:
        raise ResourceConfigurationError('max_running_tasks 不能超过 128。')
    raw_devices = override.get('gpu_devices', [])
    if not isinstance(raw_devices, list):
        raise ResourceConfigurationError('gpu_devices 必须是数组。')
    devices, ids = [], set()
    for item in raw_devices:
        if not isinstance(item, dict):
            raise ResourceConfigurationError('GPU 设备配置必须是对象。')
        identifier = item.get('id')
        backend = item.get('backend')
        selector = item.get('selector')
        if not all(isinstance(value, str) and value.strip() for value in (identifier, backend, selector)):
            raise ResourceConfigurationError('GPU 设备需要非空 id、backend 和 selector。')
        if identifier != identifier.strip() or len(identifier) > 64:
            raise ResourceConfigurationError('GPU 设备 id 无效。')
        if backend != 'cuda':
            raise ResourceConfigurationError('当前仅支持经核验的 CUDA 设备后端；Brush 需要独立设备适配器。')
        if not re.fullmatch(r'GPU-[A-Za-z0-9-]{1,128}', selector):
            raise ResourceConfigurationError('GPU selector 必须使用 nvidia-smi 返回的 GPU UUID（GPU-...），不能使用易变序号。')
        if ',' in selector or any(char.isspace() for char in selector) or '\x00' in selector:
            raise ResourceConfigurationError('GPU UUID 无效。')
        if any(device['selector'] == selector for device in devices):
            raise ResourceConfigurationError(f'GPU selector 重复：{selector}')
        if identifier in ids:
            raise ResourceConfigurationError(f'GPU 设备 id 重复：{identifier}')
        ids.add(identifier)
        devices.append({'id': identifier, 'backend': backend, 'selector': selector})
    # 路线自身需要的 GPU 后端不可被配置文件降级为 CPU；其它路线映射可显式扩展。
    method_backends = override.get('method_backends', {})
    if not isinstance(method_backends, dict) or any(
        not isinstance(method, str) or not isinstance(backend, str) or not backend.strip()
        for method, backend in method_backends.items()
    ):
        raise ResourceConfigurationError('method_backends 必须是路线到后端的对象映射。')
    return ResourceConfig(cpu_slots, max_running, tuple(devices), dict(method_backends))


class ResourceManager:
    """在数据库事务外缓存设备观察，在事务内只做确定性资源计算。"""

    def __init__(self, workspace, *, config=None, probe=None, probe_ttl=2.0):
        self.root = workspace.root if hasattr(workspace, 'root') else Path(workspace)
        self.config = load_config(self.root, config)
        self.probe = probe if probe is not None else NvidiaSmiProbe()
        self.probe_ttl = float(probe_ttl)
        self._lock = threading.RLock()
        self._observed_at = 0.0
        self._probe_result = {'status': 'unavailable', 'error': '尚未探测', 'devices': []}
        self.refresh(force=True)

    @property
    def gpu_enabled(self):
        return bool(self.config.gpu_devices)

    def refresh(self, *, force=False):
        if not self.gpu_enabled:
            return self._probe_result
        with self._lock:
            if not force and time.monotonic() - self._observed_at < self.probe_ttl:
                return self._probe_result
            try:
                value = self.probe()
                if isinstance(value, list):
                    value = {'status': 'available', 'error': None, 'devices': value}
                if not isinstance(value, dict):
                    raise ValueError('设备探测器返回值无效。')
                observations = []
                for item in value.get('devices', []):
                    if isinstance(item, DeviceObservation):
                        observations.append(item)
                    elif isinstance(item, dict):
                        observations.append(DeviceObservation(**item))
                    else:
                        raise ValueError('设备探测结果无效。')
                matched = []
                for configured in self.config.gpu_devices:
                    observation = next(
                        (item for item in observations if self._matches(configured, item)), None
                    )
                    if observation is not None:
                        matched.append(id(observation))
                if len(matched) != len(set(matched)):
                    self._probe_result = {
                        'status': 'ambiguous',
                        'error': '多个 GPU 配置项指向同一物理设备，拒绝可能的重复分配。',
                        'devices': observations,
                    }
                    self._observed_at = time.monotonic()
                    return self._probe_result
                self._probe_result = {
                    'status': value.get('status', 'unavailable'),
                    'error': value.get('error'),
                    'devices': observations,
                }
            except Exception as error:
                self._probe_result = {'status': 'unavailable', 'error': str(error), 'devices': []}
            self._observed_at = time.monotonic()
            return self._probe_result

    def request_for(self, method, parameters):
        if not isinstance(method, str):
            raise ResourceConfigurationError('任务路线无效。')
        values = parameters if isinstance(parameters, dict) else {}
        cpu_slots = _positive_int(values.get('threads', 1), 'threads')
        unverified_backend = UNVERIFIED_GPU_METHODS.get(method)
        if unverified_backend:
            raise UnsupportedGpuBackend(
                f'{unverified_backend} 路线尚无经验证的设备选择适配器，服务队列拒绝启动。'
            )
        backend = 'cuda' if method == 'fast3r' else self.config.method_backends.get(method)
        request = {
            'cpu_slots': cpu_slots,
            'gpu_required': bool(backend),
            'gpu_backend': backend,
        }
        return self.validate_request(request)

    def normalize(self, request):
        if request is None:
            return {'cpu_slots': 1, 'gpu_required': False, 'gpu_backend': None}
        if isinstance(request, str):
            try:
                request = json.loads(request)
            except json.JSONDecodeError as error:
                raise ResourceConfigurationError('任务资源请求不是有效 JSON。') from error
        if not isinstance(request, dict):
            raise ResourceConfigurationError('任务资源请求必须是对象。')
        cpu_slots = _positive_int(request.get('cpu_slots', 1), 'cpu_slots')
        gpu_required = request.get('gpu_required', False)
        if not isinstance(gpu_required, bool):
            raise ResourceConfigurationError('gpu_required 必须是布尔值。')
        backend = request.get('gpu_backend')
        if gpu_required and (not isinstance(backend, str) or not backend.strip()):
            raise ResourceConfigurationError('GPU 任务需要 gpu_backend。')
        if not gpu_required:
            backend = None
        return {'cpu_slots': cpu_slots, 'gpu_required': gpu_required, 'gpu_backend': backend}

    def validate_request(self, request):
        request = self.normalize(request)
        if request['cpu_slots'] > self.config.cpu_slots:
            raise ResourceConflict(
                f"任务需要 {request['cpu_slots']} 个 CPU slot，超过总预算 {self.config.cpu_slots}。"
            )
        if request['gpu_required']:
            if request['gpu_backend'] != 'cuda':
                raise UnsupportedGpuBackend(
                    f"{request['gpu_backend']} 后端尚无经验证的设备选择适配器，拒绝启动 GPU 任务。"
                )
            self.refresh()
            configured = [item for item in self.config.gpu_devices if item['backend'] == request['gpu_backend']]
            if not configured:
                raise ResourceConflict(f"未配置兼容的 {request['gpu_backend']} GPU 设备。")
            if self._probe_result['status'] != 'available':
                raise ResourceConflict('GPU 设备无法可靠探测，拒绝启动 GPU 任务。')
            if not any(self._matches(item, observation) for item in configured for observation in self._probe_result['devices']):
                raise ResourceConflict('配置的 GPU 设备未通过身份核验，拒绝启动 GPU 任务。')
        return request

    @staticmethod
    def _matches(configured, observation):
        return configured['selector'] in (observation.selector, observation.uuid)

    def _configured_observations(self, backend):
        observations = self._probe_result.get('devices', [])
        result = []
        for configured in self.config.gpu_devices:
            if configured['backend'] != backend:
                continue
            observation = next((item for item in observations if self._matches(configured, item)), None)
            if observation is not None:
                result.append((configured, observation))
        return result

    def allocation_candidate(self, db, request):
        """只读取已持久化账本，返回分配或等待原因。"""
        request = self.normalize(request)
        row = db.execute("""
            SELECT COALESCE(SUM(cpu_slots), 0) AS cpu,
                   COUNT(*) AS tasks
            FROM resource_allocations WHERE state='allocated'
        """).fetchone()
        if row['tasks'] >= self.config.max_running_tasks:
            return None, 'max_running_tasks'
        if request['cpu_slots'] > self.config.cpu_slots:
            return None, 'cpu_request_exceeds_budget'
        if row['cpu'] + request['cpu_slots'] > self.config.cpu_slots:
            return None, 'cpu_busy'
        if not request['gpu_required']:
            return {'cpu_slots': request['cpu_slots'], 'gpu_id': None}, None
        configured = [item for item in self.config.gpu_devices if item['backend'] == request['gpu_backend']]
        if not configured:
            return None, 'gpu_unavailable'
        if self._probe_result['status'] != 'available':
            return None, 'gpu_probe_unknown'
        candidates = self._configured_observations(request['gpu_backend'])
        if not candidates:
            return None, 'gpu_unavailable'
        used = {
            item['gpu_id'] for item in db.execute(
                "SELECT gpu_id FROM resource_allocations WHERE state='allocated' AND gpu_id IS NOT NULL"
            )
        }
        for configured, _ in candidates:
            if configured['id'] not in used:
                return {'cpu_slots': request['cpu_slots'], 'gpu_id': configured['id']}, None
        return None, 'gpu_busy'

    def waiting_reason(self, db, request):
        """返回当前余量下的等待原因；可运行任务返回 None。"""
        _, reason = self.allocation_candidate(db, request)
        return reason

    def prepare_claim(self):
        """在打开 SQLite 写事务前刷新一次设备观察。"""
        self.refresh()

    def validate_ledger(self, repository):
        """服务重启或预算变更时，拒绝把账本不一致当成空闲。"""
        configured_ids = {item['id'] for item in self.config.gpu_devices}
        with repository.connect() as db:
            usage = db.execute("""
                SELECT COALESCE(SUM(cpu_slots), 0) AS cpu
                FROM resource_allocations WHERE state='allocated'
            """).fetchone()['cpu']
            gpu_ids = {row['gpu_id'] for row in db.execute("""
                SELECT gpu_id FROM resource_allocations
                WHERE state='allocated' AND gpu_id IS NOT NULL
            """)}
        if usage > self.config.cpu_slots:
            raise ResourceConflict(
                f'持久化 CPU allocation {usage} 超过当前预算 {self.config.cpu_slots}，需要人工核查。'
            )
        unknown = sorted(gpu_ids - configured_ids)
        if unknown:
            raise ResourceConflict('持久化 GPU allocation 不在当前配置中：' + ','.join(unknown))

    def execution_env(self, task):
        request = self.normalize(task.get('resource_request'))
        env = {
            'OMP_NUM_THREADS': str(request['cpu_slots']),
            'MKL_NUM_THREADS': str(request['cpu_slots']),
            'OPENBLAS_NUM_THREADS': str(request['cpu_slots']),
            'NUMEXPR_NUM_THREADS': str(request['cpu_slots']),
        }
        allocation = task.get('allocation') or {}
        if request['gpu_required'] and allocation.get('gpu_id'):
            configured = next(
                (item for item in self.config.gpu_devices if item['id'] == allocation['gpu_id']), None
            )
            if configured and configured['backend'] == 'cuda':
                env['CUDA_VISIBLE_DEVICES'] = configured['selector']
        return env

    def snapshot(self, repository):
        self.refresh()
        with repository.connect() as db:
            usage = db.execute("""
                SELECT COALESCE(SUM(cpu_slots), 0) AS cpu,
                       COUNT(*) AS tasks
                FROM resource_allocations WHERE state='allocated'
            """).fetchone()
            allocated = {
                row['gpu_id']: row['attempt_id']
                for row in db.execute("""
                    SELECT gpu_id,attempt_id FROM resource_allocations
                    WHERE state='allocated' AND gpu_id IS NOT NULL
                """)
            }
        observations = {item.selector: item for item in self._probe_result.get('devices', [])}
        observations.update({item.uuid: item for item in self._probe_result.get('devices', []) if item.uuid})
        gpus = []
        for configured in self.config.gpu_devices:
            observation = observations.get(configured['selector']) or next(
                (item for item in self._probe_result.get('devices', [])
                 if self._matches(configured, item)), None
            )
            probe_status = self._probe_result.get('status', 'unavailable')
            if observation is None and probe_status == 'available':
                probe_status = 'unavailable'
            gpus.append({
                'id': configured['id'],
                'backend': configured['backend'],
                'allocated_to': allocated.get(configured['id']),
                'memory_total_bytes': observation.memory_total_bytes if observation else None,
                'memory_used_bytes': observation.memory_used_bytes if observation else None,
                'utilization_percent': observation.utilization_percent if observation else None,
                'probe_status': probe_status,
            })
        return {
            'cpu': {
                'total_slots': self.config.cpu_slots,
                'allocated_slots': int(usage['cpu']),
                'available_slots': max(0, self.config.cpu_slots - int(usage['cpu'])),
                'overcommitted_slots': max(0, int(usage['cpu']) - self.config.cpu_slots),
            },
            'max_running_tasks': self.config.max_running_tasks,
            'running_tasks': int(usage['tasks']),
            'gpus': gpus,
            'probe_error': self._probe_result.get('error'),
        }
