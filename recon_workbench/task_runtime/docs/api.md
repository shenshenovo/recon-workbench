# 任务运行管理 API

状态：PR1 的队列、提交、查询和取消已实现；PR2 的资源查询、资源预算和受控并行已实现；PR3 标记的接口仍是目标契约。表中 PR 表示首次交付阶段。
现有契约见仓库 `docs/COLLABORATION.md`；每个实现 PR 同步生产者、前端、旧接口测试和该文档。

## 1. 通用约定

- 服务仍只监听 loopback，保留 Host 与所有 POST 的 `X-Local-Token` 校验；GET 不泄露工作区外路径。
- 路径以任务/项目稳定 ID 定位；归属检查先于取消/恢复等操作；账号鉴权由权限模块接入。
- 时间为带时区的 UTC ISO 8601，界面按用户时区显示。PID、开始时间、队列位置允许 null，不补成0。
- 失败保留 `{"error":"中文说明"}`，新接口可附 `code`；404未知对象，409状态/幂等冲突，400参数，403校验失败。
- 老接口原来的400错误保持兼容；新409只用于新增冲突语义。旧成功字段保留，消费者适配新增可空值。
- 所有状态查询只读；浏览器关闭、刷新和切页不影响执行。

## 2. 接口清单

| PR | 接口 | 行为 |
| --- | --- | --- |
| PR1 | POST `/api/start` | 校验、固定输入并入队；不因已有任务报错 |
| PR1 | GET `/api/tasks?project_id=...` | 旧列表保留，增加队列和当前执行信息 |
| PR1 | GET `/api/task?id=...` | 任务详情含当前 attempt；PR3 增加尝试历史 |
| PR1 | GET `/api/queue?project_id=...` | 待运行项、活动任务、派发阻塞信息 |
| PR1 | GET `/api/job?task_id=...` | 指定任务的状态、日志、阶段及进度 |
| PR1 | POST `/api/cancel` | 新增明确 task_id，取消排队或运行任务 |
| PR2 | GET `/api/resources` | 配置预算、占用、可用、GPU观察信息 |
| PR2 | POST `/api/queue-resume` | 核查通过后解除派发暂停；不能跳过残留进程检查 |
| PR3 | POST `/api/task-resume-check` | 只读恢复预检，给出可复用/需重做阶段 |
| PR3 | POST `/api/task-resume` | 校验预检指纹并创建恢复尝试、入队 |

备份和恢复首版使用离线 CLI，无在线 `/api/restore`。不添加接口允许客户端指定任意执行命令或目标文件。

## 3. PR1：提交与幂等

沿用现有 method/input_type/image_set_id 及路线参数，不统一或重命名已有连字符字段。
新增可选 `idempotency_key`（1～128字符）；服务端以 project_id 和该键唯一约束。

```json
{
  "project_id": "default",
  "image_set_id": "set-示例",
  "name": "scene",
  "method": "sfm",
  "threads": 4,
  "idempotency_key": "submit-example-001"
}
```

正常入队返回200（保留旧成功码）：

```json
{
  "task_id": "task-示例",
  "status": "queued",
  "output": "outputs/scene_示例",
  "pid": null,
  "queue_position": 1,
  "waiting_reason": null
}
```

提交成功时通常为queued；重放可返回 preparing/running/终态。preparing 时 queue_position 为null。
无键每次是新任务；同键同标准化业务参数返回同一任务，参数不同返回409 `idempotency_conflict`。
标准化摘要包含项目、输入身份/所选内容指纹、路线和执行参数，不包含生成的输出名、PID或提交时间。
第一次提交固定输入；同键重放不更新输入，界面显示原提交的输入指纹；需要新输入时使用新键。
“一次点击重试”可复用键；“再次实验”必须新键或不传键。
准备阶段失败也保留原任务供查询，不能用同键悄悄创建第二个执行。

queue_position 是当前全局待运行顺序（非预计开始时间），仅 queued 有值；PR2 可以被暂时可运行任务超越。
项目筛选不改变全局排序。输出目录在计算开始前可能不存在，前端不能据此判断任务损坏。

## 4. PR1：查询与旧单任务接口兼容

`/api/queue` 返回：

```json
{
  "dispatch_paused": false,
  "blocked_reason": null,
  "running": [],
  "queued": [{"task_id": "task-示例", "queue_position": 1, "waiting_reason": "worker_busy"}]
}
```

任务详情保留旧 `id/parameters/command/states/log_text/models` 等字段，新增：

| 字段 | 类型/语义 |
| --- | --- |
| `current_attempt_id` | string或null；未派发时null |
| `queue_position` | integer或null；动态计算 |
| `waiting_reason` | string或null；worker_busy/max_running_tasks/cpu_busy/gpu_busy/gpu_probe_unknown/gpu_unavailable/dispatch_paused/reconciliation_required |
| `cancel_requested_at` | string或null |
| `attempts` | PR3起执行历史列表，不能让旧 attempts 日志混入当前进度 |
| `resource_request` | PR2起 `{cpu_slots,gpu_required,gpu_backend}` |
| `allocation` | PR2起 `{cpu_slots,gpu_id}` 或null |

`GET /api/job?task_id=...` 沿用 status/returncode/output/states/progress/log/task_id；未启动日志为空、progress为null。
无 task_id 的旧 `/api/job` 仍返回单对象：优先最早活动任务，其次队首，其次最近终态，否则idle；
PR2 附 `running_task_ids/queued_count`，全局页面通过 `/api/queue` 展示完整列表。
不能依靠无ID接口实施多任务取消，工作台必须按当前查看的 task_id 查询和操作。
网页不再因另一任务运行就禁用提交；状态字典、项目筛选和历史详情增加 preparing/queued/cancelling。

## 5. PR1：取消

```json
{"task_id": "task-示例"}
```

返回200：

```json
{"task_id":"task-示例","status":"cancelling","cancel_requested":true}
```

- queued：事务取消且不启动进程，返回 cancelled。
- preparing：取消标记阻止入队；快照操作安全收尾后 cancelled。
- starting/running：先返回 cancelling，后台清理完成后才能查到 cancelled。
- 已取消：重复返回 cancelled；已完成/失败：返回原终态，cancel_requested=false，不改旧结果。
- 缺 task_id 的旧 `{}`：仅恰好一个活动任务时可取消它；没有活动任务返回 false；多个活动任务返回409 `task_id_required`。
- 无法确认子进程退出：保持 cancelling 或中断待核查状态、保留分配，并展示错误，不返回虚假清理成功。

## 6. PR2：资源接口

配置首版在文件中修改并重启，无在线配置更新 API。提交不允许任意指定环境变量或伪造 allocation。
`threads` 同时作为任务 CPU 预算输入，GPU 路线仍需 CPU；默认沿用已校验的 threads 默认值。
如后续增加独立 cpu_slots 参数，必须同 PR 更新有效线程数映射，不能存在“申请2但运行8”的默认行为。

配置文件 `data/task_runtime/resources.json` 示例：

```json
{
  "cpu_slots": 8,
  "max_running_tasks": 3,
  "gpu_devices": [{"id": "gpu0", "backend": "cuda", "selector": "GPU-UUID"}],
  "method_backends": {"fast3r": "cuda"}
}
```

selector 使用 `nvidia-smi --query-gpu=uuid --format=csv,noheader` 查询到的 `GPU-...` UUID，不接受易变的数字序号。没有该文件时保持单执行槽和 CPU 兼容模式；显式配置 GPU 后，设备必须通过 `nvidia-smi` 身份探测才允许 GPU 任务启动。

`GET /api/resources` 示例：

```json
{
  "cpu": {"total_slots":8,"allocated_slots":4,"available_slots":4},
  "max_running_tasks":3,
  "gpus":[{
    "id":"gpu0","backend":"cuda","allocated_to":"attempt-示例",
    "memory_total_bytes":8589934592,"memory_used_bytes":null,
    "utilization_percent":null,"probe_status":"unavailable"
  }],
  "dispatch_paused":false,
  "blocked_reason":null
}
```

分配数据来自持久化账本，监控数据可为空；额度不表示系统利用率，显存观察不表示硬配额。
`POST /api/queue-resume` 的请求为 `{}`；预检通过解除工作区暂停并返回状态，残留身份/设备不确定返回409及原因。
该接口不能强制释放未知进程资源；资源不足的任务继续排队。

## 7. PR3：阶段恢复

仅失败/中断任务可恢复，queued/活动/取消/成功任务返回409；缺失输入或校验工具可返回可解释的 blocked 计划。

`POST /api/task-resume-check`：

```json
{"task_id":"task-示例"}
```

返回200（不创建尝试/不启动程序）：

```json
{
  "task_id":"task-示例",
  "can_resume":true,
  "plan_fingerprint":"sha256-示例",
  "reuse":[{"stage":"sfm","source_attempt_id":"attempt-1"}],
  "rerun":["mvs.mesh","mvs.textured"],
  "blocked_reasons":[]
}
```

`POST /api/task-resume`：

```json
{"task_id":"task-示例","plan_fingerprint":"sha256-示例"}
```

返回200 `{task_id, attempt_id, status:"queued", queue_position}`。
计划变化返回409 `resume_plan_changed`；同计划重复请求如已创建对应尝试则返回该尝试，不重复入队。
不同计划与活动尝试冲突返回409。新 attempt 沿用输入和参数，不能借恢复修改训练数据、线程或关键路线参数。
若要改变参数，创建新任务；不自动把失败任务无限重试。

## 8. PR3：离线备份 CLI

以下为计划命令，当前执行会失败，待 PR3 实现 `task_runtime/backup.py`：

```bash
# 所有命令要求服务停止，并核查无残留计算；目标应位于工作区外
python -m recon_workbench.task_runtime.backup create \
  --workspace /绝对路径/recon-workbench \
  --archive /备份目录/workspace-001.tar.gz

python -m recon_workbench.task_runtime.backup verify \
  --archive /备份目录/workspace-001.tar.gz \
  --max-unpacked-bytes 107374182400

python -m recon_workbench.task_runtime.backup restore \
  --archive /备份目录/workspace-001.tar.gz \
  --target /不存在的新目录/recon-restored \
  --max-unpacked-bytes 107374182400
```

最大展开量提供有限默认值、可调；同时限制文件数。归档/目标已存在不覆盖，不提供 --force。
退出码：0成功，1运行/校验失败，2参数错误，130中断；失败向stderr说明具体缺失/损坏/冲突文件，stdout给简短结果。
恢复只创建数据工作区，不打包应用源码或算法工具；在匹配版本代码的新目录恢复 data/outputs 后再安装/配置环境。
首次启动派发暂停，人工核对后通过 `/api/queue-resume` 解除；旧机进程不被新目录接管。

## 9. 每个 PR 的文档更新检查

PR1 核对提交返回值、状态枚举、按ID轮询、幂等和取消；PR2 补资源等待、设备能力和多任务兼容；
PR3 补恢复指纹、attempt来源、备份版本和退出码。每次附请求/响应测试及当前实现状态，禁止把未启用API列为可用。
