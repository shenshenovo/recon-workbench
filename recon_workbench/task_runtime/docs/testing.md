# PR1/PR2/PR3 测试矩阵

| 场景 | 测试 | 断言 |
| --- | --- | --- |
| 新库、重复迁移与并发 claim | `test_claim_is_single_winner_and_fifo` | schema 可重复升级；并发 claim 只有一个 winner |
| FIFO 单 worker | `test_scheduler_runs_second_after_first` | 第二任务等待，第一完成后按序执行 |
| 进程树取消 | `test_cancel_keeps_terminal_state_after_process_tree_cleanup` | 取消最终为 cancelled，活动 attempt 清零 |
| 迁移中断 | `test_migration_failure_rolls_back` | 故障后无半迁移版本，重试可成功 |
| 旧库升级 | `test_legacy_rows_survive_versioned_upgrade_and_repeat` | 旧任务和模型记录保留，重复升级幂等 |
| CPU 额度和并行 claim | PR2 `test_cpu_budget_allows_two_tasks_and_blocks_third` / `test_scheduler_runs_two_cpu_tasks_in_parallel` | CPU=8 时两个 CPU=4 attempt 并行，第三个等待 |
| 并发 claim 不超卖 | PR2 `test_concurrent_resource_claim_never_oversells_cpu` | 多线程同时 claim 最多分配8个CPU slot |
| GPU 整卡独占 | PR2 `test_gpu_allocation_is_exclusive_and_reusable` / `test_multiple_gpu_devices_are_assigned_once_each` | 单卡串行；多卡并行但每张卡只分配一次 |
| 资源超额和探测失败 | PR2 `test_gpu_probe_failure_rejects_gpu_request_but_cpu_is_valid` | GPU 探测未知时 GPU 请求拒绝，CPU 请求仍可运行 |
| 无适配设备、非法预算和未确认清理 | PR2 其余 `test_resources.py` 场景 | GPU 路线 fail-closed；清理未确认时 allocation 仍占用 |
| 阶段预检与 MVS 边界恢复 | `test_recovery.py` | dense/mesh 产物通过清单后只恢复后继阶段；损坏前置产物使依赖失效 |
| 参数/版本/输入变化 | `test_parameter_or_execution_version_changes_disable_reuse`, `test_corrupt_dependency_invalidates_downstream_and_input_blocks` | 只读预检拒绝不一致 attempt，恢复命令执行前再次校验 |
| 并发恢复和终态隔离 | `test_concurrent_resume_has_one_attempt_and_joins_queue`, `test_resume_idempotency_cancel_and_late_old_update` | 同一计划只有一个活动 attempt；旧 attempt 迟到更新不覆盖新结果 |
| 发布故障收敛 | `test_publication_database_failure_rolls_back_then_restart_converges` | 模型登记失败不切换结果指针，重启可核对并完成发布 |
| 备份资产完整性 | `test_backup.py` | DB/照片/模型缺失拒绝；归档篡改、恢复路径和新目录恢复可验证 |

执行：

```bash
python3 -m unittest discover -s recon_workbench/task_runtime/tests -t . -v
python3 -m unittest -v
```

自动化测试使用受控 Python 子进程，不能证明 COLMAP/OpenMVS/Brush 或真实 GPU 的清理行为；Windows 原生进程树和硬件验收需单独记录。

## 本地执行记录（2026-11-02）

- `.venv/bin/python -m unittest recon_workbench.task_runtime.tests.test_runtime -q`：6 项通过。
- `.venv/bin/python -m unittest -q`：94 项通过。
- `.venv/bin/python -m unittest -q recon_workbench.task_runtime.tests.test_resources recon_workbench.task_runtime.tests.test_runtime`：16 项通过。
- `.venv/bin/python -m py_compile recon_workbench/task_runtime/*.py recon_workbench/task_runtime/tests/*.py`、`git diff --check`：通过；本 PR 未修改前端。
- 临时 HTTP 服务手动 smoke：入队、执行、查询和幂等冲突 409 通过；临时 Python 任务取消后持久化 `cancelled` 通过。

- `.venv/bin/python -m unittest discover -s recon_workbench/task_runtime/tests -t . -v`：44 项通过；覆盖 PR3 阶段恢复、并发 resume、发布故障和备份恢复。
- `.venv/bin/python -m unittest -v`：122 项通过；未执行真实 COLMAP/OpenMVS/Brush/Fast3R 或真实 GPU 恢复。
