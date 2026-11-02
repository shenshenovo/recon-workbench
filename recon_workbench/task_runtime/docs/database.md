# PR1/PR2 数据库契约

迁移由 `recon_workbench.task_runtime.migrations.apply` 负责，版本写入 `task_runtime_meta(schema_version)`。迁移只增加字段和表，旧任务、模型、项目及历史目录不删除；重复执行安全。

`tasks` 新增：

| 字段 | 含义 |
| --- | --- |
| `queue_seq` | 提交时递增的全局 FIFO 序号；非 queued 历史记录可为空 |
| `idempotency_key` | 项目内可选提交键，非空唯一 |
| `request_fingerprint` | 标准化业务参数摘要，用于同键冲突检测 |
| `input_fingerprint` | 固定输入/质量筛选摘要 |
| `cancel_requested_at` | 取消请求时间 |
| `current_attempt_id` | 当前执行尝试 |
| `waiting_reason` | 可观察的等待原因，PR2 扩展 |
| `resource_request` | JSON；任务申请的 `cpu_slots`、`gpu_required` 和 `gpu_backend` |

`task_attempts` 每次执行一行，保存 `attempt_no`、命令、PID/PGID、状态、返回码、清理确认和时间。claim、任务状态和 attempt 插入在同一 `BEGIN IMMEDIATE` 事务中；终态更新带 `task_id + current_attempt_id` 条件，迟到回调不能覆盖新尝试。

PR2 新增 `resource_allocations` 资源账本：每个 attempt 最多一行，保存 CPU slot 数、独占 GPU ID、分配/释放时间和 `allocated/released` 状态。资源检查、attempt 创建、任务 claim 和 allocation 插入在同一个 `BEGIN IMMEDIATE` 事务内完成；只有进程树清理确认后才标记 `released`。未确认的 attempt 会继续占用账本并阻止冲突资源派发。

迁移中断由 SQLite 事务回滚。服务重启不接管旧 attempt；无法核查的残留使新派发暂停。`queued` 任务保留原 `queue_seq`，取消排队任务不创建进程。
