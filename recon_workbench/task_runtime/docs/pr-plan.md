# PR1 实施记录

总 Issue：[shenshenovo/recon-workbench#2](https://github.com/shenshenovo/recon-workbench/issues/2)。本分支对应 PR1，提交说明使用 `Refs #2`，不关闭总 Issue。

本分支实现 `feat(runtime): 持久化单 worker 队列与按任务取消`。PR2 的 CPU/GPU 资源账本、PR3 的阶段恢复和备份恢复仍未启用。

## 代码范围

- `migrations.py`：在旧 `workbench.sqlite3` 上执行可重复的增量迁移，新增队列字段、幂等索引和 `task_attempts`。
- `repository.py`：提交、幂等冲突、FIFO `BEGIN IMMEDIATE` claim、条件终态更新、取消和重启核查。
- `processes.py`：POSIX 进程组所有权，SIGINT → SIGTERM → SIGKILL 升级并确认退出。
- `snapshots.py`：质量筛选、输入内容哈希和原始照片/SfM/dataset 快照，staging 成功后原子发布。
- `service.py`：把现有 HTTP 参数校验接入准备、幂等占用和队列发布；`server.py::Jobs` 仅是兼容入口。
- `scheduler.py`/`processes.py`/`worker.py`：单 worker、启动握手、执行身份和进程树清理。
- `tests/test_runtime.py`：迁移回滚、并发 claim、FIFO、幂等冲突和取消清理测试。

## 已知边界

服务启动时若发现未结束的旧 attempt 会暂停派发并显示 `reconciliation_required`，不会猜测旧 PID 或自动重跑。Windows 原生进程树、WSL 启动 Windows 工具和真实算法资源不在 PR1 验收范围。
