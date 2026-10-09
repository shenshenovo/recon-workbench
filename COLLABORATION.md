# 小组协作与接口约定

本文件记录当前可运行接口，并给出建议采用的协作规则。进入小组协作后先共同确认模块负责人；新增算法、改变字段或响应格式前更新本文件及对应测试。

## 1. 当前模块边界

| 模块 | 文件 | 对外职责 |
| --- | --- | --- |
| 统一流程 | `main.py` | 选择路线、调用各阶段、汇总任务状态与结果 |
| SfM | `reconstruct.py` | 调用 COLMAP，输出相机、稀疏模型、点云和统计 |
| MVS | `densify.py` | 复用 SfM，调用 OpenMVS，输出点云/网格/纹理 |
| 3DGS 数据准备 | `prepare_brush.py` | 复用 SfM，去畸变/缩放并生成标准数据集 |
| 3DGS 训练 | `train_3dgs.py` | 调用 Brush，记录训练、划分、评估和高斯模型 |
| 评估与报告 | `evaluate.py` | 统一读取结果，生成对比记录与 JSON/CSV |
| 本地 HTTP 与任务管理 | `app.py` | 上传、启动/停止、目录与结果接口 |
| 界面 | `web/index.html` | 调用 HTTP 接口、显示进度/指标/评估图片 |
| 点云显示 | `pointcloud.py`, `web/viewer.js` | PLY 解析、抽样、WebGL 交互显示 |

建议按“算法与流程”“后端与任务”“界面与展示”“评估与报告”分工。`main.py`、`app.py`、`README.md` 是常见交叉修改点，修改前在小组中说明范围。

## 2. 统一 CLI 接口

```bash
python3 main.py --method sfm --images 图片目录 --output 新任务目录
python3 main.py --method mvs --sfm-output 已有SfM目录 --output 新任务目录
python3 main.py --method 3dgs --dataset Brush数据集目录 --output 新任务目录
```

- `--method`：`sfm` / `mvs` / `3dgs`。
- 输入三选一：`--images`、`--sfm-output`、`--dataset`。SfM 只接受照片；MVS 接受照片或已有 SfM；3DGS 三种输入均可。
- `--output` 必须不存在，不能与输入目录重叠；省略则创建时间戳目录。
- 输入照片和已有结果不改写。结果目录失败后保留，重跑创建新目录。
- 退出码：`0` 成功，`1` 执行失败，`2` CLI 参数错误，`130` 中断。
- `--dry-run` 打印命令；它不等价于工具检查或真实算法测试。

新增算法时优先新增独立脚本，再接入 `main.py::build_plan()`。当前不是动态插件架构，新增路线还需要同步更新 HTTP 参数校验和前端选项。

## 3. 文件与状态接口

统一任务根目录：

```text
任务目录/
  config.json       # 本次请求参数
  status.json       # 当前状态和阶段记录
  summary.json      # 成功后生成的结果摘要
  logs/             # 各阶段日志
  sfm/              # 从照片开始时生成
  mvs/              # MVS 路线
  3dgs/             # 3DGS 路线
```

`status.json` 公共字段：`status`（running/completed/failed/cancelled）、`started_at`、`elapsed_seconds`、`stages`。执行后可出现 `current_stage`、`finished_at`、`error`、`summary`。阶段条目包含 `name`、`status`、`command`、`log`，结束后包含耗时及退出码。

`summary.json` 当前字段按路线不同：

| 路线 | 主要结果字段 |
| --- | --- |
| SfM | `input_images`, `registered_images`, `registration_ratio`, `selected_model`, `points3D`, `pointcloud`, `mean_point_reprojection_error_px` |
| MVS | SfM 字段，以及 `dense_pointcloud`, `dense_points`, `colmap_pointcloud`；网格阶段包含 `mesh`, `mesh_faces`；纹理阶段包含 `textured_mesh`, `texture_material`, `texture_images` |
| 3DGS | `dataset`, `initial_points`, `steps`, `splats`, `gaussian_ply`, `exports`, `evaluation`, `split_file` |

统一入口额外加入 `method`、`task_output`、`stages`、`route_elapsed_seconds`。独立脚本的 `elapsed_seconds` 与整条路线耗时不完全相同。

现有 JSON 中很多路径是本机绝对路径，不能直接作为跨电脑交换格式。共享代码时使用相对路径命令；交换结果时应带上实际文件并修正路径，不能只发送 JSON。对比记录通过 `evaluate.collect_run()` 归一化，界面使用 HTTP 的 `id`（例如 `outputs/my-task`），不要自行猜结果文件路径。

## 4. HTTP 接口

服务默认 `http://127.0.0.1:8765`，仅本机访问。JSON 变更请求发送 `Content-Type: application/json` 与 `X-Local-Token`；token 由本次服务启动生成并注入页面，不写入仓库。失败响应为 `{"error":"说明"}`，常用状态码为 400/403/404。

| 方法与路径 | 请求 | 响应 |
| --- | --- | --- |
| GET `/api/runs` | 无 | 已完成任务数组，包含 `id`、方法、指标、结果路径、评估曲线等 |
| GET `/api/job` | 无 | 当前界面任务状态、阶段、日志、可用时的 `progress` |
| GET `/api/folders?path=data` | 项目相对目录 | `path`, `folders[{name,path}]` |
| POST `/api/start` | 见下方示例 | `output`（项目相对路径）, `pid` |
| POST `/api/cancel` | `{}` | `cancel_requested` |
| POST `/api/upload-new` | `{}` | 上传批次 `id`, `path` |
| POST `/api/upload-file?id=...&name=...` | 原始图片字节；携带 token | 成功上传的 `files` 数量和输入 `path` |
| GET `/api/cloud?run=outputs/...&kind=dense` | 任务 id，kind 为 dense 或 sparse | 二进制点云包，格式见下一节 |
| GET `/api/previews?run=outputs/...` | 任务 id | 评估图片相对路径数组 |
| GET `/api/image?path=outputs/.../x.png` | 结果图片相对路径 | JPG/PNG 图片字节 |
| POST `/api/report` | `{"runs":["outputs/任务A","outputs/任务B"]}` | 报告目录 `path` |
| POST `/api/open` | `{"run":"outputs/任务A","viewer":false}` | `opened`；viewer=true 且为 3DGS 时启动 Brush |

启动请求示例：

```json
{
  "name": "courtyard",
  "method": "3dgs",
  "input_type": "dataset",
  "input": "data/brush/courtyard",
  "steps": 5000,
  "max-image-size": 1200,
  "max-splats": 1000000,
  "export-every": 1000,
  "eval-every": 1000,
  "eval_split_every": 8,
  "seed": 42,
  "save_eval_images": true,
  "with_viewer": false
}
```

注意 HTTP 的 `input_type` 使用 `images` / `sfm-output` / `dataset`；部分参数用连字符，部分用下划线，以上是现有实现。上传后将 `input_type` 设为 `images`，`input` 使用上传返回的目录。原始照片可额外指定 `camera_model`；MVS 可指定 `mvs_stage`。

当前每个服务实例只管理一个运行中任务。`/api/runs` 展示已完成任务，不是完整的持久化任务队列。服务器重启后历史完成结果仍可扫描；进程控制只针对本次界面启动的当前任务。

## 5. 点云二进制接口

`/api/cloud` 返回：

1. 前 4 字节：little-endian uint32，表示 JSON 头长度。
2. 对应长度的 UTF-8 JSON，以空格补齐到 4 字节边界。
3. 后续为每点 6 个 little-endian float32：`x,y,z,r,g,b`，RGB 在 0～1 范围。

JSON 字段：`original_points`, `display_points`, `min`, `max`, `fit_min`, `fit_max`, `sampled`。最多显示 30 万点，抽样只用于预览。主体聚焦使用坐标分位范围，不改变原始重建文件。

这里只渲染普通点云。Gaussian PLY 含有高斯参数，不能复用这个接口假装实现 3DGS 渲染。后续网页高斯查看应另设渲染模块。

## 6. 评估约定

- 缺失指标保留 null，前端显示“—”，不补为 0。
- SfM 重投影误差、MVS 几何真值误差、3DGS PSNR/SSIM 是不同指标，不互相替代。
- 3DGS 保存 `split.json`（实际视图名单及条件）和 `evaluation.json`（逐步指标）。当前是已知位姿条件下的视图留出评估。
- 比较 3DGS 时查看 `comparison_group` 和完整配置；分组只核对已记录条件，不自动控制所有变量。
- 短训练和自动测试验证工程流程，不能写成正式重建质量结论。

## 7. 协作与提交

建议使用独立分支，如 `feature/web-viewer`、`feature/evaluation`，通过 Pull Request 合入 main。接口变更说明触发场景、变更字段和验证方式，并同步修改调用方。

提交源码、网页资源、文档和测试；不提交数据集、结果文件、外部程序、编译缓存或凭据。`.gitignore` 已排除 `data/`、`outputs/`、`.tools/`、`target/` 和 `.env`。组员应分别安装 COLMAP、OpenMVS、Brush，并各自下载或取得有授权的数据。

检查命令：

```bash
python3 -m unittest test_reconstruct test_prepare_brush test_train_3dgs test_main test_evaluate test_app test_pointcloud
python3 main.py --method sfm --images data/自己的照片 --dry-run
```

Python 测试不要求下载数据集或安装算法程序；真实 CPU/GPU 重建需在本机另做验证。当前本地工具发现与文件夹打开行为主要面向 macOS，其他系统需要指定工具路径并验证适配。
