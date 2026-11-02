# PR1/PR2/PR3 实施记录

总 Issue：[shenshenovo/recon-workbench#2](https://github.com/shenshenovo/recon-workbench/issues/2)。本分支按用户要求继续承载 PR1/PR2/PR3，提交说明使用 `Refs #2`，不关闭总 Issue。

本分支实现 PR1 的持久化队列与取消清理，以及 PR2 的 CPU/GPU 资源账本和受控并行。PR3 增加只读阶段预检、恢复入队、独立 attempt 输出和离线备份恢复；本次仅后端，不修改前端、不合并分支。

## 代码范围

- `migrations.py`：在旧 `workbench.sqlite3` 上执行可重复的增量迁移，新增队列字段、幂等索引和 `task_attempts`。
- `repository.py`：提交、幂等冲突、FIFO `BEGIN IMMEDIATE` claim、条件终态更新、取消和重启核查。
- `processes.py`：POSIX 进程组所有权，SIGINT → SIGTERM → SIGKILL 升级并确认退出。
- `snapshots.py`：质量筛选、输入内容哈希和原始照片/SfM/dataset 快照，staging 成功后原子发布。
- `service.py`：把现有 HTTP 参数校验接入准备、幂等占用和队列发布；`server.py::Jobs` 仅是兼容入口。
- `scheduler.py`/`processes.py`/`worker.py`：单 worker、启动握手、执行身份和进程树清理。
- `resources.py`：校验 CPU/GPU 配置和设备身份，维护资源观察、线程环境和分配候选。
- `migrations.py`/`repository.py`/`scheduler.py`：资源请求与 allocation 账本、事务 claim、多 worker 调度和释放。
- `tests/test_runtime.py`：迁移回滚、并发 claim、FIFO、幂等冲突和取消清理测试。

## PR2 已知边界

GPU 资源控制需要显式配置 `data/task_runtime/resources.json`；没有配置时保持 CPU 兼容模式，Fast3R 会因缺少设备配置而拒绝提交。GPU 监控依赖 `nvidia-smi`，监控数据不可用时不会把设备当成可启动资源。Brush 暂无经验证的设备选择适配器，服务队列会拒绝 3DGS GPU 任务；原直接 CLI 行为不变。

## 已知边界

服务启动时若发现未结束的旧 attempt 会暂停派发并显示 `reconciliation_required`，不会猜测旧 PID 或自动重跑。Windows 原生进程树、WSL 启动 Windows 工具和真实算法资源不在 PR1 验收范围。

## PR3 实现和演示

- `recovery.py`/`validators.py`：输入、参数、执行版本、SHA-256 清单和阶段依赖检查。
- `recovery_worker.py`：排队后启动前再验证；SfM 整段重做、MVS dense/mesh 后继恢复、3DGS 准备复用与整段训练、Fast3R 整段重做。
- `repository.py`/`scheduler.py`：新 attempt 原子入队，保留旧产物，成功才切换 `tasks.output` 并登记模型。
- `backup.py`：共享工作区锁、SQLite backup API、资产关联校验、版本/链接/路径/大小/哈希检查和新目录发布。
- `test_recovery.py`、`test_backup.py`：临时工作区、真实 Python worker/HTTP、数据库发布故障及恶意归档负向测试。

操作先调用 `POST /api/task-resume-check`，再将 `task_id` 和返回的 `plan_fingerprint` 传入 `POST /api/task-resume`。离线命令见 [api.md](api.md)。
备份只覆盖 `data/` 与 `outputs/`，数据库引用这些目录外的资产会明确拒绝；原件应由用户自行整理，工具不会自动搬动。
恢复生成的是数据目录，需在该目录另放置相同版本代码和配置算法环境；旧 command 保留审计，不直接运行。
恢复会将原 queued 任务保留为 `interrupted`，保留队列序号；需要逐项恢复预检和重新入队，再解除工作区派发暂停。
真实算法和硬件验收没有完成，因此总 Issue 保持 OPEN，不使用 Closes。
