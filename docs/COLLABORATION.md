# 小组协作与接口约定

业务代码位于 `recon_workbench/`，测试位于 `tests/`，前端位于 `web/`。以下命令从仓库根目录执行。根目录 `app.py` / `main.py` 仅负责启动，修改 HTTP 或调度逻辑时分别修改 `recon_workbench/server.py` / `recon_workbench/pipeline.py`。

本文件记录当前可运行接口，并给出建议采用的协作规则。进入小组协作后先共同确认模块负责人；新增算法、改变字段或响应格式前更新本文件及对应测试。

## 1. 当前模块边界

| 模块 | 文件 | 对外职责 |
| --- | --- | --- |
| 统一流程 | `main.py` → `recon_workbench/pipeline.py` | 选择路线、调用各阶段、汇总任务状态与结果 |
| SfM | `recon_workbench/reconstruct.py` | 调用 COLMAP，输出相机、稀疏模型、点云和统计 |
| MVS | `recon_workbench/densify.py` | 复用 SfM，调用 OpenMVS，输出点云/网格/纹理 |
| 3DGS 数据准备 | `recon_workbench/prepare_brush.py` | 复用 SfM，去畸变/缩放并生成标准数据集 |
| 3DGS 训练 | `recon_workbench/train_3dgs.py` | 调用 Brush，记录训练、划分、评估和高斯模型 |
| 评估与报告 | `recon_workbench/evaluate.py` | 统一读取结果，生成对比记录与 JSON/CSV |
| 本地 HTTP 与任务管理 | `app.py` → `recon_workbench/server.py` | 上传、启动/停止、目录与结果接口 |
| 项目元数据 | `recon_workbench/workspace.py` | SQLite 中保存项目、图片集、任务与模型关联 |
| 图片质量 | `recon_workbench/image_quality.py` | 解码、模糊/曝光/重复提示、校验筛选名单、复制不可变任务输入 |
| 重建工作台 | `web/index.html`, `web/workbench.js` | 分步骤输入、设置、后台状态与点云结果 |
| 项目记录界面 | `web/projects.html`, `web/projects.js` | 图片集、全部历史、任务详情与模型归集 |
| 实验对比界面 | `web/experiments.html`, `web/experiments.js` | 按来源和路线筛选、条件核对、指标与报告 |
| 共享界面 | `web/common.js`, `web/styles.css` | API、项目上下文、全局任务入口、导航与样式 |
| 图片筛选界面 | `web/quality.js` | 检测阈值、报告与勾选、保存确认、启动前校验 |
| 点云显示 | `recon_workbench/pointcloud.py`, `web/viewer.js` | PLY 解析、抽样、WebGL 交互显示 |

建议按“算法与流程”“后端与任务”“界面与展示”“评估与报告”分工。`recon_workbench/pipeline.py`、`recon_workbench/server.py`、`docs/COLLABORATION.md` 是常见交叉修改点，修改前在小组中说明范围。

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

新增算法时优先新增独立脚本，再接入 `recon_workbench/pipeline.py::build_plan()`。当前不是动态插件架构，新增路线还需要同步更新 HTTP 参数校验和前端选项。

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

`status.json` 公共字段：`status`（running/completed/failed/cancelled）、`started_at`、`elapsed_seconds`、`stages`。执行后可出现 `current_stage`、`finished_at`、`error`、`summary`。阶段条目包含 `name`、`status`、`command`、`log`，新任务还记录 `cwd`，结束后包含耗时及退出码。

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

页面路由：`/` 和 `/reconstruct` 为工作台，`/projects` 为项目记录，`/experiments` 为实验对比。三页独立加载脚本；共享 `project` 查询参数与当前项目选择，历史结果通过 `/reconstruct?project=项目ID&task=任务ID` 打开，复用输入通过 `input=图片集ID` 打开。静态资源采用显式白名单，不将文件系统路径暴露为通用静态服务。改变服务端路由后需重启原服务再刷新页面。

工作台草稿按项目保存在当前标签 sessionStorage；包括输入、参数、未保存勾选及检测阈值。恢复未保存勾选时先核对报告指纹；仍需明确保存后才能启动。页面跳转不调用取消接口；所有页面通过 `/api/job` 提供运行任务的返回入口。

| 方法与路径 | 请求 | 响应 |
| --- | --- | --- |
| GET `/api/runs?project_id=...` | 项目 ID 可省略 | 已完成任务数组，包含原有 `id`、新增 `task_id`/`project_id`、指标和评估曲线 |
| GET `/api/job` | 无 | 当前界面任务状态、阶段、日志、可用时的 `progress` |
| GET `/api/folders?path=data` | 项目相对目录 | `path`, `folders[{name,path}]` |
| POST `/api/start` | 见下方示例；可带 `project_id`/`image_set_id` | `output`（项目相对路径）, `pid`, `task_id` |
| POST `/api/cancel` | `{}` | `cancel_requested` |
| POST `/api/upload-new` | `{"project_id":"项目ID","name":"照片名称"}`，均可省略 | 上传批次 `id`, `path`, 持久化 `image_set_id` |
| POST `/api/upload-file?id=...&name=...` | 原始图片字节；携带 token | 成功上传的 `files` 数量和输入 `path` |
| GET `/api/cloud?run=outputs/...&kind=dense` | 任务 id，kind 为 dense 或 sparse | 二进制点云包，格式见下一节 |
| GET `/api/previews?run=outputs/...` | 任务 id | 评估图片相对路径数组 |
| GET `/api/image?path=outputs/.../x.png` | 结果图片相对路径 | JPG/PNG 图片字节 |
| POST `/api/report` | `{"runs":["outputs/任务A","outputs/任务B"]}` | 报告目录 `path` |
| POST `/api/open` | `{"run":"outputs/任务A","viewer":false}` | `opened`；viewer=true 且为 3DGS 时启动 Brush |
| GET `/api/projects` | 无 | 项目数组：`id`, `name`, `created_at`, 图片集数和任务数 |
| POST `/api/projects` | `{"name":"校园建筑"}` | 新项目：`id`, `name`, `created_at` |
| POST `/api/project-rename` | `{"id":"项目ID","name":"新名称"}` | 修改后的项目 |
| GET `/api/project?id=...` | 项目 ID | 项目资料与 `image_sets`, `tasks`, `models` |
| POST `/api/image-sets` | `project_id`, `path`, `input_type`, 可选 `name` | 登记已有项目内输入目录；返回图片集记录 |
| GET `/api/source-image?set=...&name=...` | 图片集 ID 和记录中的图片名 | 该图片集的 JPG/PNG 字节 |
| GET `/api/tasks?project_id=...` | 项目 ID 可省略 | 全部状态的任务数组 |
| GET `/api/task?id=...` | 稳定任务 ID | 任务记录、`states`, `log_text` 与模型记录 |
| GET `/api/quality?image_set_id=...` | 原始图片集 ID | `image_set_id`, `supported`, `report`, `analyzed_at`, `selection` |
| POST `/api/quality-analyze` | `image_set_id`, 可选 `thresholds` | 同上；同步执行，最多检测 500 张 |
| POST `/api/image-selection` | `image_set_id`, `fingerprint`, `names` | 同上，保存确认后的名单；至少两张可解码图片 |

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

注意 HTTP 的 `input_type` 使用 `images` / `sfm-output` / `dataset`；部分参数用连字符，部分用下划线，以上是现有实现。上传后将 `input_type` 设为 `images`，`input` 使用上传返回的目录。原始照片可额外指定 `camera_model`、`camera_params`（英文逗号分隔的内参字符串）和 `single_camera`（布尔值）；MVS 可指定 `mvs_stage`。

CLI 对应 `--camera-model`、`--camera-params`、`--single-camera`，统一入口向 SfM 透传。内参必须对应原始图片尺寸和所选模型；鱼眼参数顺序为 `fx,fy,cx,cy,k1,k2,k3,k4`，OPENCV 为 `fx,fy,cx,cy,k1,k2,p1,p2`，PINHOLE 为 `fx,fy,cx,cy`，SIMPLE_RADIAL 为 `f,cx,cy,k`。检查数量、有限数值与正焦距；留空继续使用 COLMAP 的 EXIF/默认初始化。提供内参属于标定先验，不包含相机位姿；COLMAP 默认仍会优化内参。只有相同镜头、焦距与图像尺寸才应共享内参。网页切换输入或相机模型时清空旧的内参，防止跨数据集误用。

匹配命令退出成功后还会检查 `two_view_geometries` 是否有非空图像对；全部为空时报告几何验证失败并给出相机/重叠提示，不再直接进入 mapper。详细工具原因保留在 `logs/02_matching.log`。

当前一个工作目录仅允许启动一个服务，同一时间运行一个任务，没有等待队列。同目录第二个服务即使换端口也会提示已有服务，避免两个实例互相改写任务状态。

`data/workbench.sqlite3` 保存元数据（已被 `.gitignore` 排除），文件仍存放在原来的 `data/` 与 `outputs/`。SQLite 中资产路径是项目相对路径；原始参数、调用命令和算法 JSON 仍可能包含本机绝对路径。

项目默认 ID 为 `default`，已有顶层实验在启动时幂等导入此项目，保留原文件；包含失败、取消和未正常结束的任务。导入的历史任务优先使用原始 `started_at`，没有时采用文件夹修改时间，因此不应将所有历史记录时间视为精确启动时间。后续通过 CLI 创建的结果在下次服务启动时登记。

`project_id` → `image_set_id` → `task_id` → 模型记录 `id` 为稳定关联。`/api/start` 缺省项目为 `default`；带 `image_set_id` 时从记录读取 `input` 和 `input_type`，忽略请求中的这两个字段，并检查图片集属于项目。新图片集的 `input_type` 可以是 `images` / `dataset` / `sfm-output`；后两种表示复用输入，不保证都有图片清单。上传批次 `id` 仅在当前进程有效，`image_set_id` 和已上传照片长期保留。

图片集记录的 `count` 始终表示原始总数；`selected_count` 表示已保存的筛选数量，未确认时为 null。`selection_status` 为 `unchecked` / `pending` / `saved` / `not_applicable`。下拉框和图片集列表使用“已选 8 / 总计 14 张”区分两者；未保存的临时勾选不会改写 `selected_count`。

**原始照片启动接口变化：** 网页/HTTP 的 `images` 输入必须先检测并保存筛选，否则 `/api/start` 返回 400。先由上传或 `/api/image-sets` 得到图片集 ID，再调用 `/api/quality-analyze`，使用返回的 `report.fingerprint` 和选中 `names` 调用 `/api/image-selection`，最后 `/api/start` 传入 `project_id`、`image_set_id` 与路线参数。`dataset` / `sfm-output` 输入及原有 CLI 不受此门槛影响。

检测阈值 `thresholds` 可包含 `blur_threshold`（默认 60）、`dark_mean`（40）、`bright_mean`（215）、`near_distance`（整数 4）。分析尺度固定为最长边最多 512；截断暗/亮像素门限分别为灰度 ≤20 / ≥235，占比阈值为 0.6；SHA-256 相同才标为文件完全重复。dHash 距离只对亮度差 ≤10、宽高比差 ≤2%、灰度标准差 ≥8 的候选匹配，不作为自动剔除条件。

报告 `report` 包含 `version`, `pillow_version`, `policy`, `fingerprint`, `counts`, `images`。每张图片有 `name`, `path`, `sha256`, `bytes`, `decodable`, `flags`，可解码时还有尺寸、`laplacian_variance`, `mean_luminance`, `dark_ratio`, `bright_ratio`, `dhash` 等；重复项有 `duplicate_of`，相似项有 `similar_to`/`hash_distance`。标志为 `decode_error`, `blurry`, `underexposed`, `overexposed`, `exact_duplicate`, `near_duplicate`。这些标志可重叠，不是质量排名或经过标定的错误概率。

新增 `quality_reports` / `image_selections` 表，启动时通过 `CREATE TABLE IF NOT EXISTS` 扩展已有数据库，不删除原记录。`selection` 包含 `fingerprint`, `names`, `updated_at`；首次分析不自动确认。重复检测内容和阈值均未变化时保留确认；变化后清除确认。追加上传会同时清除旧报告与确认。保存筛选拒绝重复、未知和不可解码图片名；修改文件在启动复制时按内容哈希再次校验。

网页任务使用 `data/task_inputs/任务目录名/images/` 中的独立副本，保留相对子目录与原始字节；同级 `selection.json` 保存来源、文件名单和哈希，`quality.json` 保存当次完整报告。任务 `parameters` 新增 `source_input`, `selected_images`, `quality_fingerprint`, `quality_manifest`，`input` 指向实际副本，`command` 也使用副本路径。不能以只记录前端勾选、却仍把整个原目录交给 COLMAP 的方式实现筛选。

同一进程的检测、筛选保存、上传写入与输入复制使用锁避免内部状态竞争；工作区外部的文件修改则在复制时通过哈希检测。图片集和模型仍是本地资源关联，当前没有账号权限控制。质量接口需要 Pillow，组员先在虚拟环境中安装 `requirements.txt`，测试见 `TESTING_QUALITY.md`。

任务保存 `parameters`, `command`, `output`, `log`, `pid`, `returncode`, `error`, `stage`, `elapsed` 和时间字段。状态为 `starting` / `running` / `completed` / `failed` / `cancelled` / `interrupted`。后台线程在子进程退出后保存最终状态，不依赖浏览器轮询。正常 Ctrl+C 会停止当前任务并保存结果；异常关闭后，下次启动读取原始任务状态：已有完成/失败/取消结果据实恢复，其他未结束任务标记 `interrupted`。不会根据旧 PID 自动接管或终止进程；异常关闭后原进程可能仍在运行，需检查日志和系统进程，再创建新任务。

`/api/job` 仍表示本次服务当前执行任务；历史详情使用 `/api/task`。`/api/runs` 是已完成任务的评估列表，全部状态的持久化历史使用 `/api/tasks`。模型记录保存 `task_id`, `kind`, `path`, `bytes`，只登记结果目录内实际存在的文件，纹理附件也包含在内；目前支持查看其来源任务，下载和账号权限将在后续阶段实现。

## 5. 点云二进制接口

`/api/cloud` 返回：

1. 前 4 字节：little-endian uint32，表示 JSON 头长度。
2. 对应长度的 UTF-8 JSON，以空格补齐到 4 字节边界。
3. 后续为每点 6 个 little-endian float32：`x,y,z,r,g,b`，RGB 在 0～1 范围。

JSON 字段：`original_points`, `display_points`, `min`, `max`, `fit_min`, `fit_max`, `sampled`。最多显示 30 万点，抽样只用于预览。主体聚焦使用坐标分位范围，不改变原始重建文件。

这里只渲染普通点云。Gaussian PLY 含有高斯参数，不能用普通点云展示替代高斯渲染。后续网页高斯查看应另设渲染模块。

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
python3 -m unittest
python3 main.py --method sfm --images data/自己的照片 --dry-run
```

Python 测试不要求下载数据集或安装算法程序；真实 CPU/GPU 重建需在本机另做验证。当前本地工具发现与文件夹打开行为主要面向 macOS，其他系统需要指定工具路径并验证适配。
