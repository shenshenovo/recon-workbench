# PR1 测试矩阵

| 场景 | 测试 | 断言 |
| --- | --- | --- |
| 新库、重复迁移与并发 claim | `test_claim_is_single_winner_and_fifo` | schema 可重复升级；并发 claim 只有一个 winner |
| FIFO 单 worker | `test_scheduler_runs_second_after_first` | 第二任务等待，第一完成后按序执行 |
| 进程树取消 | `test_cancel_keeps_terminal_state_after_process_tree_cleanup` | 取消最终为 cancelled，活动 attempt 清零 |
| 迁移中断 | `test_migration_failure_rolls_back` | 故障后无半迁移版本，重试可成功 |
| 旧库升级 | `test_legacy_rows_survive_versioned_upgrade_and_repeat` | 旧任务和模型记录保留，重复升级幂等 |

执行：

```bash
python3 -m unittest discover -s recon_workbench/task_runtime/tests -t . -v
python3 -m unittest -v
```

自动化测试使用受控 Python 子进程，不能证明 COLMAP/OpenMVS/Brush 或真实 GPU 的清理行为；Windows 原生进程树和硬件验收需单独记录。

## 本地执行记录（2026-11-01）

- `.venv/bin/python -m unittest recon_workbench.task_runtime.tests.test_runtime -q`：6 项通过。
- `.venv/bin/python -m unittest -q`：83 项通过。
- `.venv/bin/python -m py_compile ...`、`git diff --check`：通过；`node --check` 检查三个前端脚本通过。
- 临时 HTTP 服务手动 smoke：入队、执行、查询和幂等冲突 409 通过；临时 Python 任务取消后持久化 `cancelled` 通过。
