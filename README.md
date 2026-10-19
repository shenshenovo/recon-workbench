# Recon Workbench｜多路线三维重建与可视化系统

本地三维重建工作台：上传照片、检测质量并筛选，选择 SfM / MVS / 3DGS，查看任务进度、点云与实验结果。项目、图片集、任务和模型记录使用 SQLite 保存。

本项目负责流程调度、参数适配、任务管理、质量检测、结果展示和评估记录；底层 SfM、MVS、3DGS 分别由 COLMAP、OpenMVS、Brush 执行。当前主要在 Apple Silicon macOS 上验证，网页服务仅监听本机。

## 快速启动

建议 Python 3.11+。在仓库根目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python app.py
```

打开新终端后先激活 `.venv`。重建需另行安装 COLMAP；MVS 与 3DGS 分别需要 OpenMVS 和 Brush，安装及路径说明见 [完整使用指南](docs/USAGE.md)。

网页流程：创建项目 → 上传或选择照片 → 检测质量 → 勾选并保存筛选 → 开始重建 → 查看结果。选择框会区分“已选 8 / 总计 14 张”；启动后只复制选中图片作为任务输入，原图保留。

界面分为三个独立页面：

| 页面 | 用途 |
| --- | --- |
| `/reconstruct` 重建工作台 | 图片与筛选 → 重建设置 → 运行与结果，大尺寸点云查看 |
| `/projects` 项目记录 | 图片集、全部任务历史与模型来源；打开历史结果或复用输入 |
| `/experiments` 实验对比 | 按来源和路线选择实验、核对条件、查看指标并导出报告 |

切换页面不会停止计算，顶部有运行任务入口。当前浏览器标签中的未保存筛选草稿会保留；已保存的筛选仍由 SQLite 持久化。历史结果使用任务 ID 定位，不会因点击查看而重新重建。前端重构后需要重启原来的 `app.py` 服务并刷新页面以加载新路由。

## 目录

```text
recon-workbench/
├── app.py                  # 网页启动入口
├── main.py                 # 统一重建 CLI 入口
├── recon_workbench/        # Python 业务代码
│   ├── server.py           # HTTP、上传和子进程任务管理
│   ├── pipeline.py         # SfM / MVS / 3DGS 路线调度
│   ├── reconstruct.py      # COLMAP SfM
│   ├── densify.py          # OpenMVS 稠密点云、网格、纹理
│   ├── prepare_brush.py    # 3DGS 数据准备
│   ├── train_3dgs.py       # Brush 训练适配
│   ├── workspace.py        # 项目、图片集、任务和模型持久化
│   ├── image_quality.py    # 图片检测、筛选和输入快照
│   ├── evaluate.py         # 指标整理、对比报告
│   ├── pointcloud.py       # 网页点云数据
│   ├── ply_compatible.py   # PLY 格式适配
│   └── paths.py            # 仓库根路径
├── web/                    # HTML、JavaScript、WebGL 查看器
├── tests/                  # 自动化测试
├── scripts/                # 样例生成等辅助工具
├── docs/                   # 使用指南、协作接口、手动验收
├── requirements.txt        # 图片处理依赖 Pillow
├── data/                   # 本机照片、数据库、筛选快照（不提交）
├── outputs/                # 本机模型、日志、报告（不提交）
└── .tools/                 # 本机 OpenMVS / Brush（不提交）
```

`.venv/` 是本地 Python 环境，`target/` 是 Brush 运行时产生的缓存，均已忽略。整理目录时没有迁移已有数据、模型和数据库，原有历史任务仍使用原路径。

## 命令行与测试

两个常用启动命令保持不变：

```bash
python app.py
python main.py --method sfm --images data/自己的照片 --output outputs/新的任务目录
python main.py --method mvs --sfm-output outputs/已有SfM目录 --output outputs/新的MVS目录
python main.py --method 3dgs --dataset data/brush/自己的数据集 --output outputs/新的3DGS目录
```

单独执行算法、评估与工具时使用模块入口，避免直接运行包内文件：

```bash
python -m recon_workbench.reconstruct --help
python -m recon_workbench.densify --help
python -m recon_workbench.prepare_brush --help
python -m recon_workbench.train_3dgs --help
python -m recon_workbench.evaluate --scan outputs
python -m scripts.make_quality_demo --output data/新的质量样例目录
```

```bash
python -m unittest
python -m unittest tests.test_image_quality
```

测试使用临时文件和本地端口，不要求安装算法工具；真实重建需另做环境与数据验证。网页中的原始照片任务要求先保存质量筛选，CLI 可独立运行。

## 文档与协作

- [完整使用指南](docs/USAGE.md)：安装、数据集、各条路线、参数与已有实测说明。
- [模块、接口与协作约定](docs/COLLABORATION.md)：模块分工、HTTP/CLI、状态与文件格式。
- [图片质量检测手动验收](docs/TESTING_QUALITY.md)：生成样例、筛选、重启恢复和实际输入校验。
- [页面逻辑与测试扩展规划](docs/UI_PLAN.md)：三页职责、页面验收、正式实验与数据集候选。
- [数据集选择与分层测试计划](docs/DATASET_TEST_PLAN.md)：三个真实场景、下载入口、数量阶梯、损坏与退化样例及结果记录。

协作时按模块建立分支，通过 Pull Request 合并。源码在 `recon_workbench/`，测试放入 `tests/`，接口变更同步更新 `docs/COLLABORATION.md`。GitHub 不包含照片、模型、数据库、外部工具或虚拟环境；组员需各自配置本机环境。

当前尚未实现账号权限、并行任务队列、网页网格/高斯渲染和模型下载。质量提示属于启发式检查；没有几何真值或统一评估条件时，不据此宣称重建精度提升。
