# PR1/PR2/PR3 数据库契约

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

`task_attempts` 每次执行一行，保存 `attempt_no`、命令、PID/PGID、状态、返回码、清理确认和时间。PR3 另保存 `output_path`、`log_path`、来源 attempt、恢复计划指纹、输入/参数/执行版本指纹；恢复尝试始终写入独立 `attempts/<attempt_id>` 目录。claim、任务状态和 attempt 插入在同一 `BEGIN IMMEDIATE` 事务中；终态更新带 `task_id + current_attempt_id` 条件，迟到回调不能覆盖新尝试。

PR2 新增 `resource_allocations` 资源账本：每个 attempt 最多一行，保存 CPU slot 数、独占 GPU ID、分配/释放时间和 `allocated/released` 状态。资源检查、attempt 创建、任务 claim 和 allocation 插入在同一个 `BEGIN IMMEDIATE` 事务内完成；只有进程树清理确认后才标记 `released`。未确认的 attempt 会继续占用账本并阻止冲突资源派发。

迁移中断由 SQLite 事务回滚。服务重启不接管旧 attempt；无法核查的残留使新派发暂停。`queued` 任务保留原 `queue_seq`，取消排队任务不创建进程。

## PR3 文件一致性

任务输出和中间阶段在退出后写入 `artifact_manifest.json`，记录普通文件的相对路径、大小和 SHA-256。恢复预检同时验证输入快照、参数摘要、执行代码/工具版本和阶段依赖；只复用完整清单通过的产物。数据库从 v2 增量升级到 v3；回填输出/日志位置，新增参数和执行指纹仅对新 attempt 记录。旧 attempt 缺少指纹时保守重做，历史任务和模型记录不删除。

离线备份使用 SQLite backup API、完整性/外键检查和工作区独占锁。归档清单覆盖 `data/`、`outputs/` 内受管文件，并逐项核对 SQLite 的图片集、输入快照、任务结果、模型和恢复来源；缺少关联照片或模型会拒绝生成。恢复只能发布到不存在的新目录，验证哈希后重写已知工作区前缀，保留任务与队列序号，将原 queued 转为 interrupted，保留原 command 审计且持久化暂停派发；由新环境显式预检恢复后重新生成命令。
