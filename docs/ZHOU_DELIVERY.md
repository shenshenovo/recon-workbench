# 周政炀任务交付与验收索引

依据 README 链接的 TEAM_TASK_PLAN.md。提交范围：重建路线说明、增量 SfM、不可变输入、父子模型版本，以及必要的结果读取修复与验收材料。

| 分工要求 | 实现与对应材料 |
|---|---|
| 页面路线选择、结果、用途、前置条件与资源限制 | web/index.html、workbench.js；SfM→MVS/3DGS 关系提示，Fast3R 独立提示 |
| 选择依据与实测支持 | INCREMENTAL_SFM.md、SOUTH_BUILDING_ACCEPTANCE.md、DOWNSTREAM_ACCEPTANCE.md；几何检查用 SfM，稠密几何用 MVS，新视角渲染用 3DGS；Fast3R 的 CUDA/显存提示来自既有 FAST3R.md 与 GPU_EXPERIMENTS.md |
| 补图扩展已有 SfM | --base-sfm 与 HTTP parent_task_id；复制父数据库、模型和图片，仅提取新增特征，继续 mapper 并允许整体优化 |
| 保存新旧版本 | 独立输出目录与 SQLite 父任务字段；SHA-256 校验旧输入，拒绝重复补图；父任务不覆盖 |
| 50→70 增量/全量对照 | SOUTH_BUILDING_ACCEPTANCE.md；50/50、70/70、70/70 注册；增量 578.536 秒，全量 1054.163 秒 |
| MVS 按新结果重建 | DOWNSTREAM_ACCEPTANCE.md；70 张新版 SfM，351916 稠密点，139.007 秒；截图 acceptance/mvs-result.png |
| 3DGS 新版重新训练 | DOWNSTREAM_ACCEPTANCE.md；Windows Brush 500 步、59681 高斯，已校验 Gaussian PLY |
| 旧训练状态续训另行验证 | 当前没有提供续训能力，明确记录为独立后续验证，不把普通 PLY 当作训练状态 |
| 代码、必要测试、设计、实际记录 | 本分支源码与 tests；增量设计、两份实测记录、复现和验证脚本；照片/数据库/工具/模型不纳入 Git |

## 本机网页闭环验收

2026-11-05 在实际浏览器选择 added2 图片集，检测并保存 2/2 筛选，选择 incremental-base6 父任务、SIMPLE_RADIAL，从页面启动 web-incremental-acceptance。CLI 的 50→70 正式实验与网页小样例各自验证不同环节，不能相互替代。

网页任务 completed，8/8 注册、6251 个点、25.87 秒，页面成功加载点云；截图见 [网页增量结果](acceptance/web-incremental-result.png)。稠密点云页面实测显示 175958/351916 点，见 [MVS 结果](acceptance/mvs-result.png)。

历史稀疏/稠密结果均已实际打开检查；嵌套实验目录读取问题已修复，结果不可用时显示明确错误。运行状态附加特征提取与匹配日志，避免 CPU 块匹配期间页面无阶段日志。

## 复现

最终回归：85 项测试运行，结果 OK，2 项跳过。另有入口/调度针对性复测 8 项通过。可提交精简数据见 [实测结果 JSON](acceptance/measured-results.json)，不包含本机绝对路径或原图/模型。

```bash
python -m unittest
python -m scripts.run_incremental_benchmark --images data/scene/images --output outputs/new_comparison
python -m scripts.verify_incremental_benchmark outputs/new_comparison
```

需要 COLMAP 的真实实验与不依赖算法工具的自动化测试分开执行。不同机器先检查 Python 和算法版本；旧任务没有输入指纹时需重新生成基线。MVS 本机只编译/验收 dense 阶段；3DGS 500 步是流程验证，没有最终渲染质量结论；没有几何真值，不声称精度提升。

## 远端与集成

2026-11-05 fetch 后 origin/main 为 127c485，与本次功能基线一致。origin/feat/task-runtime-plan 更新到 e1f3d59，含持久化队列、资源调度、恢复备份；origin/smq_bk 为 2b60d64，含账号权限和 UI 更新。它们尚未合并到 main，本分支不合入这些独立功能。

合并热点：server.py 的 Jobs/HTTP、workspace.py 的 schema/任务记录、workbench.js/index.html/projects.js 的 UI。集成时需保留 parent_task_id、原图筛选快照、阶段日志、嵌套结果目录读取，并在队列启动实际子进程时重新校验父输入。数据库迁移应合并各分支新增字段；账号分支必须继续对父任务和父模型做归属检查。
