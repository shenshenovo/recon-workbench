# 任务运行管理

当前状态：**PR1 已实现**：SQLite 持久化 FIFO 单 worker、幂等提交、按任务取消和受控进程树清理。PR2/PR3 仍为规划。

这里集中存放你负责的任务队列、CPU/GPU 调度、进程清理、阶段恢复和备份恢复代码。
PR1 已提供 `migrations.py`、`repository.py`、`snapshots.py`、`service.py`、`scheduler.py`、`processes.py` 和 `worker.py`；资源、阶段恢复和备份模块按后续 PR 添加。

## 从哪里开始

1. [issue.md](docs/issue.md)：总任务、边界和三个 PR 的可勾选验收标准。
2. [pr-plan.md](docs/pr-plan.md)：三个 PR 的提交顺序、文件范围和演示步骤。
3. [design.md](docs/design.md)：状态机、进程所有权、调度和恢复设计。
4. [api.md](docs/api.md)：按 PR 标记的 HTTP/CLI 目标契约。
5. [database.md](docs/database.md)：增量迁移、任务尝试、资源账本和文件格式。
6. [testing.md](docs/testing.md)：测试矩阵和实际运行记录模板。
7. [tests/README.md](tests/README.md)：专属测试的组织及运行方式。

## 预计结构

下面标记“计划”的文件由后续功能 PR 创建，不代表现在已经可用。

```text
recon_workbench/task_runtime/
  __init__.py
  README.md
  docs/
    issue.md
    pr-plan.md
    api.md
    database.md
    design.md
    testing.md
  tests/
    __init__.py
    README.md
    test_queue.py           # PR1 计划
    test_processes.py       # PR1 计划
    test_api.py             # PR1 起持续补充
    test_migrations.py      # PR1 起持续补充
    test_resources.py       # PR2 计划
    test_recovery.py        # PR3 计划
    test_backup.py          # PR3 计划
  migrations.py            # PR1 计划：版本化增量迁移
  repository.py            # PR1 计划：事务、claim、执行尝试和条件更新
  scheduler.py             # PR1 计划：单 worker；PR2 扩展资源调度
  processes.py             # PR1 计划：进程树所有权和取消升级
  service.py               # PR1：Workspace/HTTP 与运行时适配
  snapshots.py             # PR1：输入快照、哈希和原子发布
  worker.py                # PR1：登记后放行算法命令的包装进程
  resources.py             # PR2 计划：CPU/GPU 需求、预留、释放
  recovery.py              # PR3 计划：阶段检查及恢复计划
  validators.py            # PR3 计划：各路线产物校验
  backup.py                # PR3 计划：离线备份、验证和恢复 CLI
```

## 公共模块边界

专属目录不是第二套后端。`server.py` 继续负责 HTTP，`workspace.py` 继续持有现有工作区数据，
`pipeline.py` 继续负责路线编排；通过小型适配调用本包，避免复制这些公共模块。
现有算法代码、网页和 `tests/` 仅在接入新状态、参数或执行器时做必要修改。

专属测试集中在本目录是本任务的组织约定；旧测试仍保留在仓库 `tests/`。
所有路径、命令均从仓库根目录执行。不要将数据库、照片、模型、进程状态或备份压缩包提交到本包。

## 三个功能 PR

| 顺序 | 建议分支 | 核心交付 |
| --- | --- | --- |
| PR1 | `feat/task-queue` | 持久化 FIFO 队列、单 worker、按任务取消与进程树清理 |
| PR2 | `feat/task-resources` | CPU 额度、整卡 GPU 独占、受控并行、资源账本 |
| PR3 | `feat/task-recovery-backup` | 校验后的阶段恢复、离线一致性备份和新目录恢复 |

每个 PR 都必须包含实现、能发现边界缺陷的测试，以及本目录三个核心文档和公共协作文档的更新。
规划分支 `feat/task-runtime-plan` 不算额外的功能 PR；规划文件可作为 PR1 的首个提交。
依次合并后再从更新的 `main` 开下一分支，不把未合并的 PR2/PR3 混入 PR1。
