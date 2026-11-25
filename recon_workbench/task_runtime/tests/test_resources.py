import subprocess
import sys
from pathlib import Path
import tempfile
import threading
import time
import unittest

from recon_workbench.task_runtime.repository import TaskRepository
from recon_workbench.task_runtime.resources import (
    DeviceObservation,
    ResourceConflict,
    ResourceManager,
    UnsupportedGpuBackend,
)
from recon_workbench.workspace import Workspace


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = Workspace(self.root)
        self.repo = TaskRepository(self.workspace)

    def tearDown(self):
        self.temp.cleanup()

    def manager(self, *, gpus=(), cpu=8, max_running=3, probe=None, methods=None):
        config = {
            'cpu_slots': cpu,
            'max_running_tasks': max_running,
            'gpu_devices': list(gpus),
        }
        if methods is not None:
            config['method_backends'] = methods
        return ResourceManager(self.workspace, config=config, probe=probe or (lambda: {
            'status': 'available',
            'error': None,
            'devices': [],
        }))

    def task(self, name, request):
        task = self.workspace.create_task(
            'default', None, name, 'fixture',
            self.root / 'outputs' / name,
            self.root / 'logs' / (name + '.log'),
            {}, command=[subprocess.list2cmdline(['true'])], status='preparing',
        )
        self.assertTrue(self.repo.publish(task['id'], {}, ['true'], 'fp-' + name, request))
        return self.repo.get(task['id'])

    def test_cpu_budget_allows_two_tasks_and_blocks_third(self):
        resources = self.manager()
        tasks = [self.task(name, {'cpu_slots': 4, 'gpu_required': False}) for name in ('a', 'b', 'c')]
        first = self.repo.claim_next(resources)
        second = self.repo.claim_next(resources)
        self.assertEqual({first['id'], second['id']}, {tasks[0]['id'], tasks[1]['id']})
        self.assertIsNone(self.repo.claim_next(resources))
        snapshot = resources.snapshot(self.repo)
        self.assertEqual(snapshot['cpu']['allocated_slots'], 8)
        self.assertEqual(snapshot['running_tasks'], 2)
        with self.repo.connect() as db:
            waiting = resources.waiting_reason(db, self.repo.get(tasks[2]['id'])['resource_request'])
        self.assertEqual(waiting, 'cpu_busy')
        self.assertTrue(self.repo.finish(first['id'], first['current_attempt_id'], 'failed', 1))
        third = self.repo.claim_next(resources)
        self.assertEqual(third['id'], tasks[2]['id'])

    def test_concurrent_resource_claim_never_oversells_cpu(self):
        resources = self.manager(max_running=4)
        tasks = [self.task(str(index), {'cpu_slots': 4, 'gpu_required': False}) for index in range(4)]
        claimed = []
        lock = threading.Lock()

        def claim():
            result = self.repo.claim_next(resources)
            with lock:
                claimed.append(result)

        threads = [threading.Thread(target=claim) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(value is not None for value in claimed), 2)
        self.assertEqual(resources.snapshot(self.repo)['cpu']['allocated_slots'], 8)

    def test_gpu_allocation_is_exclusive_and_reusable(self):
        gpu = {'id': 'gpu0', 'backend': 'cuda', 'selector': 'GPU-test'}
        resources = self.manager(
            gpus=(gpu,),
            probe=lambda: {'status': 'available', 'error': None,
                            'devices': [DeviceObservation(selector='0', uuid='GPU-test')]},
            methods={'fixture': 'cuda'},
        )
        first_task = self.task('gpu-a', {'cpu_slots': 2, 'gpu_required': True, 'gpu_backend': 'cuda'})
        second_task = self.task('gpu-b', {'cpu_slots': 2, 'gpu_required': True, 'gpu_backend': 'cuda'})
        first = self.repo.claim_next(resources)
        self.assertEqual(first['allocation']['gpu_id'], 'gpu0')
        self.assertIsNone(self.repo.claim_next(resources))
        with self.repo.connect() as db:
            self.assertEqual(resources.waiting_reason(db, second_task['resource_request']), 'gpu_busy')
        self.assertTrue(self.repo.finish(first['id'], first['current_attempt_id'], 'failed', 1))
        second = self.repo.claim_next(resources)
        self.assertEqual(second['id'], second_task['id'])
        self.assertEqual(resources.execution_env(second)['CUDA_VISIBLE_DEVICES'], 'GPU-test')

    def test_multiple_gpu_devices_are_assigned_once_each(self):
        gpus = (
            {'id': 'gpu-a', 'backend': 'cuda', 'selector': 'GPU-a'},
            {'id': 'gpu-b', 'backend': 'cuda', 'selector': 'GPU-b'},
        )
        resources = self.manager(
            gpus=gpus,
            probe=lambda: {'status': 'available', 'error': None, 'devices': [
                DeviceObservation(selector='0', uuid='GPU-a'),
                DeviceObservation(selector='1', uuid='GPU-b'),
            ]},
            methods={'fixture': 'cuda'},
        )
        tasks = [self.task(name, {'cpu_slots': 1, 'gpu_required': True, 'gpu_backend': 'cuda'})
                 for name in ('gpu-a-task', 'gpu-b-task', 'gpu-c-task')]
        first = self.repo.claim_next(resources)
        second = self.repo.claim_next(resources)
        self.assertEqual({first['allocation']['gpu_id'], second['allocation']['gpu_id']}, {'gpu-a', 'gpu-b'})
        self.assertIsNone(self.repo.claim_next(resources))
        self.assertEqual(resources.snapshot(self.repo)['running_tasks'], 2)

    def test_gpu_probe_failure_rejects_gpu_request_but_cpu_is_valid(self):
        resources = self.manager(
            gpus=({'id': 'gpu0', 'backend': 'cuda', 'selector': 'GPU-test'},),
            probe=lambda: {'status': 'unavailable', 'error': 'no nvidia-smi', 'devices': []},
        )
        with self.assertRaises(ResourceConflict):
            resources.validate_request({'cpu_slots': 1, 'gpu_required': True, 'gpu_backend': 'cuda'})
        self.assertEqual(
            resources.validate_request({'cpu_slots': 2, 'gpu_required': False}),
            {'cpu_slots': 2, 'gpu_required': False, 'gpu_backend': None},
        )

    def test_gpu_routes_fail_closed_without_supported_device_adapter(self):
        cpu_only = self.manager()
        with self.assertRaises(ResourceConflict):
            cpu_only.request_for('fast3r', {'threads': 2})
        with self.assertRaises(UnsupportedGpuBackend):
            cpu_only.request_for('3dgs', {'threads': 2})

    def test_invalid_budget_and_selector_configuration_is_rejected(self):
        with self.assertRaises(ResourceConflict):
            self.manager(cpu=0)
        with self.assertRaises(ResourceConflict):
            self.manager(gpus=({'id': 'gpu0', 'backend': 'cuda', 'selector': '0,1'},))

    def test_cleanup_unknown_keeps_allocation_until_reconciled(self):
        resources = self.manager()
        task = self.task('unknown', {'cpu_slots': 4, 'gpu_required': False})
        claimed = self.repo.claim_next(resources)
        self.assertTrue(self.repo.finish(task['id'], claimed['current_attempt_id'], 'failed', 1, cleanup_confirmed=False))
        self.assertEqual(resources.snapshot(self.repo)['cpu']['allocated_slots'], 4)
        self.repo.reconcile_attempt(
            {'id': claimed['current_attempt_id'], 'task_id': task['id']},
            True,
            '已确认退出',
        )
        self.assertEqual(resources.snapshot(self.repo)['cpu']['allocated_slots'], 0)

    def test_scheduler_runs_two_cpu_tasks_in_parallel(self):
        resources = self.manager(max_running=3)
        code = (
            "from pathlib import Path; import json,sys,time; "
            "out=Path(sys.argv[1]); out.mkdir(parents=True,exist_ok=True); "
            "(out/'status.json').write_text(json.dumps({'status':'completed'})); "
            "(out/'summary.json').write_text(json.dumps({'method':'fixture'})); "
            "time.sleep(.35)"
        )
        tasks = []
        for name in ('parallel-a', 'parallel-b', 'parallel-c'):
            task = self.workspace.create_task(
                'default', None, name, 'fixture',
                self.root / 'outputs' / name,
                self.root / 'logs' / (name + '.log'),
                {}, command=[sys.executable, '-c', code, str(self.root / 'outputs' / name)],
                status='preparing',
            )
            self.assertTrue(self.repo.publish(
                task['id'], {}, task['command'], 'fp-' + name,
                {'cpu_slots': 4, 'gpu_required': False},
            ))
            tasks.append(task)
        from recon_workbench.task_runtime.scheduler import Scheduler
        scheduler = Scheduler(
            self.workspace,
            resource_config={'cpu_slots': 8, 'max_running_tasks': 3, 'gpu_devices': []},
            popen=subprocess.Popen,
        )
        scheduler.start()
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                queue = scheduler.snapshot()
                if len(queue['running']) == 2 and len(queue['queued']) == 1:
                    break
                time.sleep(.02)
            self.assertEqual(len(scheduler.snapshot()['running']), 2)
            self.assertEqual(len(scheduler.snapshot()['queued']), 1)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and any(
                    self.repo.get(task['id'])['status'] != 'completed' for task in tasks):
                time.sleep(.02)
            self.assertTrue(all(self.repo.get(task['id'])['status'] == 'completed' for task in tasks))
        finally:
            scheduler.close()


if __name__ == '__main__':
    unittest.main()
