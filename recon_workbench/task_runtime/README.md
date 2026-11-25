# 任务运行管理

当前状态：**PR1、PR2、PR3 已实现**：SQLite 持久化队列、按任务取消、CPU 额度、整卡 GPU 账本、受控并行、校验后的阶段恢复和离线备份恢复。

这里集中存放你负责的任务队列、CPU/GPU 调度、进程清理、阶段恢复和备份恢复代码。
PR1 已提供队列、快照和进程清理模块；PR2 新增 `resources.py` 并扩展迁移、仓储、服务和调度；PR3 新增阶段预检、独立 attempt 恢复、产物清单和离线备份恢复。

## 从哪里开始

1. [issue.md](docs/issue.md)：总任务、边界和三个 PR 的可勾选验收标准。
2. [pr-plan.md](docs/pr-plan.md)：三个 PR 的提交顺序、文件范围和演示步骤。
3. [design.md](docs/design.md)：状态机、进程所有权、调度和恢复设计。
4. [api.md](docs/api.md)：按 PR 标记的 HTTP/CLI 目标契约。
5. [database.md](docs/database.md)：增量迁移、任务尝试、资源账本和文件格式。
6. [testing.md](docs/testing.md)：测试矩阵和实际运行记录模板。
7. [tests/README.md](tests/README.md)：专属测试的组织及运行方式。

## 代码结构

当前测试以 test_runtime.py 汇总 PR1 队列/进程/迁移场景；真实 COLMAP/OpenMVS/Brush/Fast3R 恢复仍需按机器环境单独验收。

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
    test_runtime.py         # PR1 队列/进程/迁移；API 另在 tests/test_workspace.py
    test_resources.py       # PR2
    test_recovery.py        # PR3
    test_backup.py          # PR3
  migrations.py            # PR1：版本化增量迁移
  repository.py            # PR1：事务、claim、执行尝试和条件更新
  scheduler.py             # PR1 单 worker；PR2 扩展资源调度
  processes.py             # PR1：进程树所有权和取消升级
  service.py               # PR1：Workspace/HTTP 与运行时适配
  snapshots.py             # PR1：输入快照、哈希和原子发布
  worker.py                # PR1：登记后放行算法命令的包装进程
  resources.py             # PR2：CPU/GPU 需求、探测、预留、释放
  recovery.py              # PR3：阶段检查及恢复计划
  recovery_worker.py       # PR3：执行前再次验证
  validators.py            # PR3：各路线产物校验
  backup.py                # PR3：离线备份、验证和恢复 CLI
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
当前按用户要求继续在 feat/task-runtime-plan 分支递进实现、统一推送，不合并。后续正式拆分/审查由团队安排。
