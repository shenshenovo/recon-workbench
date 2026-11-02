# PR1/PR2 实施记录

总 Issue：[shenshenovo/recon-workbench#2](https://github.com/shenshenovo/recon-workbench/issues/2)。本分支对应 PR1/PR2，提交说明使用 `Refs #2`，不关闭总 Issue。

本分支实现 PR1 的持久化队列与取消清理，以及 PR2 的 CPU/GPU 资源账本和受控并行。PR3 的阶段恢复和备份恢复仍未启用。

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
