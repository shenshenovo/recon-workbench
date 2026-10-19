# Recon Workbench 使用指南

以下命令均在仓库根目录执行，先按根目录 [README](../README.md) 配置环境。

本项目支持照片上传、图片质量检测与筛选、COLMAP SfM、OpenMVS 稠密重建、Brush 3DGS 训练、网页点云查看和实验评估。统一入口为 `main.py`，本地界面为 `app.py`；图片质量功能的手动验收见 [TESTING_QUALITY.md](TESTING_QUALITY.md)。

小组协作前先阅读 [模块、接口与协作约定](COLLABORATION.md)。GitHub 只提交源码、文档和测试，数据集、重建产物与外部程序不随仓库分发。

图片输入 → CPU SIFT 特征提取 → 特征匹配与几何验证 → SfM / Bundle Adjustment → 稀疏点云 → 去畸变 → MVS 稠密点云 → 表面网格 → 纹理。

已在 Apple Silicon Mac 上验证。重建 CLI 使用 Python 标准库，界面图片质量检测新增 Pillow 依赖，不需要安装 PyTorch。默认输出稀疏几何；通过 `--stage dense`、`mesh`、`textured` 可以继续生成稠密点云、表面网格、纹理模型。COLMAP/OpenMVS 路线按 CPU 运行，Brush 使用 GPU 训练。

## 1. 环境

建议使用 Python 3.11+，先创建项目虚拟环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

每次打开新终端先执行 `source .venv/bin/activate`，再运行 `python app.py`。需要重建时安装 COLMAP，已安装 Homebrew 时：

```bash
brew install colmap
python3 -m recon_workbench.reconstruct --check-env
```

脚本读取实际安装版本的帮助，兼容 `SiftExtraction/SiftMatching` 与 `FeatureExtraction/FeatureMatching` 两类 CPU 参数名称。不支持的版本会在重建前报错。

## 2. 准备照片

先用 20～40 张同一静态物体或场景的照片测试。相邻照片应有较大重叠，同时移动相机获得不同观察位置，避免只在原地转动。优先拍纹理丰富、不反光、不透明的物体，保持光照稳定。

- 支持 JPG、JPEG、PNG、TIF、TIFF、BMP，递归读取子目录，忽略隐藏文件。
- HEIC 需要自行先转为 JPG/PNG。网页输入仅支持 JPG/PNG，并在检测时完整解码、识别损坏文件；直接运行 CLI 时，损坏图片由 COLMAP 处理并记入日志。
- 默认特征提取最大边长 1600，原图不会改写。
- 顺序匹配按文件名排序：请使用 `0001.jpg`、`0002.jpg` 等补零名称。
- 只有相同镜头、焦距、分辨率的照片才使用 `--single-camera`；混用手机镜头、变焦或裁剪时不要使用。

## 3. 运行

在本项目目录下执行，将输入路径替换成自己的图片文件夹：

```bash
python3 -m recon_workbench.reconstruct --images "/你的图片目录"
```

默认创建 `outputs/时间戳/`。指定结果位置：

```bash
python3 -m recon_workbench.reconstruct \
  --images "/你的图片目录" \
  --output "outputs/my_first_scene" \
  --max-image-size 1600 \
  --threads 8
```

输出目录必须不存在，也不能位于图片目录内部；不会覆盖旧结果。重新运行请使用新的输出目录。本版没有自动断点续跑，失败时保留已有产物用于排查。

按时间顺序拍摄的大量照片或视频抽帧，可以改为邻近匹配：

```bash
python3 -m recon_workbench.reconstruct \
  --images "/你的有序图片目录" \
  --matcher sequential \
  --overlap 10
```

无序照片使用默认的 `exhaustive`。该方式两两匹配，大量图片会明显变慢。运行期间查看终端显示的日志路径，或在另一个终端运行 `tail -f 输出目录/logs/03_mapper.log`。

更多参数：`python3 -m recon_workbench.reconstruct --help`。Ctrl+C 会停止当前 COLMAP 子进程，任务状态记为 `cancelled`，退出码为 130；失败退出码为 1。

## 4. 输出与查看

```text
outputs/时间戳/
├── config.json          # 参数、实际 CPU 选项和输入路径
├── status.json          # 任务状态、当前阶段、耗时、错误信息
├── summary.json         # 成功时生成：注册图片数、点数、误差等
├── image_list.txt       # 本次输入图片的相对路径
├── colmap_help.txt      # 安装版本的特征提取帮助
├── database.db          # 特征、匹配等 COLMAP 数据
├── sparse/              # 所有重建模型的原始二进制结果
│   └── 0/               # cameras.bin、images.bin、points3D.bin 等
├── model_txt/           # 所选模型的相机内参、位姿、三维点文本
├── sparse.ply           # 所选模型的彩色稀疏点云
└── logs/                # 每个阶段的命令和完整日志
```

如果产生多个独立模型，按注册图片数、再按点数选取最大的有效模型导出；其余模型保留，统计中会提示。部分图片注册成功也可能完成任务，请查看 `registration_ratio` 和 `warnings`，不要把任务完成理解成全部图片重建成功。

启动 `colmap gui`，通过 **File → Import model** 导入 `summary.json` 中 `selected_model` 指向的目录，即可查看点云及相机。`sparse.ply` 也可以交给支持 PLY 点云的查看器。

`model_txt/images.txt` 中的四元数和平移表示 **世界坐标到相机坐标**，并非直接的相机世界位置。单目 SfM 的尺度和坐标方向未做物理标定，不能直接把坐标值当作米。`mean_point_reprojection_error_px` 是 COLMAP 各点平均重投影误差的算术平均，不是真实三维几何精度。

## 5. 排查

- 没生成模型：查看 `logs/03_mapper.log`，检查重叠、纹理和相机平移；不要用重复照片冒充多视角。
- 只有部分照片注册：先使用两两匹配，移除模糊照片，补拍相邻视角；所有独立模型都会保留。
- 内存压力大：减少图片数量，使用 `--max-image-size 1200 --max-features 4096 --threads 4`。
- 稀疏点云有空洞：这是稀疏重建的正常限制；完整表面需要之后接 MVS 或其他路线。

## 6. 已完成的验证

2026-10-08，在本机 Apple M4 / 16GB、COLMAP 4.2.1 上，使用官方 South Building 数据集按文件名排序的前 20 张照片完成真实端到端测试：

- 配置：CPU、4 线程、特征提取最大边长 1200、两两匹配。
- 注册图片：20/20；三维点：5,595；平均点重投影误差：约 0.530 像素。
- 重建及导出总耗时：约 26.2 秒，不含安装、下载和环境检查；其他照片的耗时和效果会不同。
- 核对了五个执行阶段、PLY 顶点数量、相机参数文本和统计文件。
- 8 项回归检查通过，包括输出目录保护、版本参数适配、子进程失败处理、含空格路径和多模型选择；同时在系统 Python 3.9 上通过。

本地已经保留示例照片及结果。直接重跑示例（自动建立新的结果目录）：

```bash
python3 -m recon_workbench.reconstruct \
  --images data/south-building-20/images \
  --max-image-size 1200 \
  --threads 4
```

现有结果：`outputs/south-building-20/sparse.ply`；详细指标：`outputs/south-building-20/summary.json`。

运行回归检查：

```bash
python3 -m unittest -v tests.test_reconstruct
```

示例源：[COLMAP 官方数据集说明](https://colmap.github.io/datasets.html)，South Building 照片由 Christopher Zach 提供。[原始下载](https://github.com/colmap/colmap/releases/download/3.11.1/south-building.zip)。示例照片和生成结果位于 Git 忽略目录，未包含在代码中。

## 7. 稠密重建、网格与纹理

稀疏重建的特征提取、匹配、相机估计、三角化和 Bundle Adjustment 都由 COLMAP 执行。COLMAP 也负责去畸变，之后调用 OpenMVS 的 CPU 实现：

| 阶段 | 实际执行工具 | 结果 |
| --- | --- | --- |
| 去畸变及相机转换 | COLMAP `image_undistorter` | 去畸变照片和 PINHOLE 相机 |
| 格式转换 | OpenMVS `InterfaceCOLMAP` | `scene.mvs` |
| 稠密重建 | OpenMVS `DensifyPointCloud` | 深度图、`dense.mvs`、`dense.ply` |
| 表面重建 | OpenMVS `ReconstructMesh` | `mesh.mvs`、`mesh.ply` |
| 纹理映射 | OpenMVS `TextureMesh` | `textured.obj`、材质及纹理图片 |

Python 负责流程编排、参数设置、依赖检查、任务状态、日志与结果统计。代码没有自行实现这些重建算法。

### OpenMVS 环境

本机已将官方 OpenMVS v2.4.0 macOS arm64 包解压到 `.tools/openmvs/`，脚本会自动寻找它。其他机器可从 [官方 Releases](https://github.com/cdcseacave/openMVS/releases) 获取对应平台包，解压后使用 `--openmvs-bin 可执行文件所在目录`，也支持 PATH 中的工具。

```bash
python3 -m recon_workbench.densify --check-env --stage textured
```

只有选择的阶段所需工具才会检查。预检在计算前完成；依赖缺失时会明确报错。

### 复用已经算好的 SfM

你已经计算的 ETH3D courtyard 结果在 `outputs/20261008_234029_607758/`，其中 36/38 张照片注册成功。直接继续生成稠密点云：

```bash
python3 -m recon_workbench.densify \
  --sfm-output outputs/20261008_234029_607758 \
  --stage dense \
  --threads 4
```

将 `--stage dense` 改为 `--stage mesh`，会继续生成表面网格；改为 `--stage textured`，会继续生成带纹理的 OBJ 模型。每次新建输出目录，原来的 SfM 结果保持可用；原始照片和稀疏模型需要仍位于 `config.json` / `summary.json` 记录的位置。

### 从照片直接跑完整路线

```bash
python3 -m recon_workbench.reconstruct \
  --images data/eth3d/courtyard/images \
  --camera-model PINHOLE \
  --stage textured \
  --max-image-size 1200 \
  --dense-max-image-size 1200 \
  --threads 4
```

`--max-image-size` 只控制 SfM 的特征提取；`--dense-max-image-size` 控制去畸变输出和稠密计算。稠密阶段默认额外缩小一半（`--dense-resolution-level 1`），适合先验证流程；改为 `0` 会增加细节、运行时间和内存使用。

### 查看新增结果

新结果目录包含 `undistorted/`、`dense/`、`logs/` 和状态、统计文件：

- `dense/dense.ply`：彩色稠密点云，在 MeshLab 中通过 File → Import Mesh 导入。
- `dense/dense_colmap.ply`：用于 COLMAP GUI 的兼容点云，通过 File → Import model from… 导入。OpenMVS 的原始文件使用 `uint8` 颜色类型，COLMAP 的读取器要求 `uchar`；兼容导出保留全部点的 XYZ/RGB，移除法线及视图列表等附加属性。算法后续阶段仍使用原始 MVS 数据。
- `dense/mesh.ply`：带三角面的表面网格。
- `dense/textured.obj`：带纹理模型；OBJ、MTL 和纹理图片必须一起保留，导入 OBJ 后查看。
- `dense/*.dmap`：OpenMVS 中间深度图。
- `status.json`：如果网格或纹理阶段失败，`dense_result` 保留已完成的稠密点云/网格路径。

`summary.json` 中 `points3D` 仍表示稀疏点数，`dense_points` 表示稠密点数，`pointcloud` 指向本次最终点云。单独执行 `recon_workbench/densify.py` 时，`elapsed_seconds` 仅统计后续阶段，`source_sfm_elapsed_seconds` 记录源 SfM 耗时。

稠密重建只能恢复照片中有足够重叠、可匹配的可见表面，仍可能有空洞和噪点。网格未做额外 RefineMesh 优化。复用稀疏结果时只处理已注册照片；提高图像分辨率也不能恢复没有拍到的背面。

已有结果无需重新重建，可以单独导出兼容点云（输出文件必须不存在）：

```bash
python3 -m recon_workbench.ply_compatible \
  outputs/你的结果目录/dense/dense.ply \
  outputs/你的结果目录/dense/dense_colmap.ply
```

### 2026-10-09 实测

已验证两种入口：

- 复用 ETH3D courtyard 的已有 SfM：36 张已注册照片、13,773 个稀疏点，生成 290,739 个稠密点、108,398 个网格顶点和 215,982 个三角面，成功导出 OBJ / MTL / JPG 纹理。后续流程约 96.9 秒，不含原先 SfM 耗时；配置为 CPU 4 线程、最大边长 1200、额外缩小级别 1。结果目录：`outputs/eth3d-courtyard-final/`。
- 从 South Building 的 20 张照片执行 `recon_workbench/reconstruct.py --stage dense`：20/20 张注册成功，生成 171,611 个稠密点。结果目录：`outputs/south-building-dense/`。
- 核对了任务状态、阶段退出码、PLY 顶点与面数量、OBJ 面数、材质引用的实际纹理文件；12 项回归检查通过。

OpenMVS 项目文件显式使用 native binary archive（`--archive-type 1`），以保存后续步骤需要的点可见性及网格数据。网格与纹理阶段直接读取前一步的原生 MVS 项目，避免再次导入 PLY 导致当前官方构建丢失状态或崩溃。

## 整理 Brush / 3DGS 输入

`recon_workbench/prepare_brush.py` 复用已有 SfM 的相机位姿和稀疏点，通过 COLMAP 去畸变、缩放照片并同步更新内参。这个步骤准备训练输入；3DGS 训练由后续 Brush 阶段执行。

ETH3D courtyard 的整理命令：

```bash
python3 -m recon_workbench.prepare_brush \
  --sfm-output outputs/20261008_234029_607758 \
  --output data/brush/courtyard \
  --max-image-size 1200
```

输出目录必须不存在。以后处理其他场景，替换 `--sfm-output` 和 `--output` 即可；源目录中的照片、`config.json`、`summary.json` 和稀疏模型需要保留。也可以指定成功完成的 `recon_workbench/densify.py` 结果目录，脚本仍复用其原始 SfM 模型。

整理后的结构：

```text
data/brush/courtyard/
├── images/                 # 已注册照片，保留模型引用的相对路径
├── sparse/0/
│   ├── cameras.bin         # 与输出照片尺寸对应的 PINHOLE 内参
│   ├── images.bin          # 相机位姿及观测
│   └── points3D.bin        # 训练初始化用稀疏点
├── dataset.json            # 数量、相机参数和源数据说明
├── config.json
├── status.json
└── logs/
```

COLMAP 新版本还可能输出 `rigs.bin` / `frames.bin`，脚本会一起保留。数据集使用实际文件复制，迁移到其他电脑时带走整个目录即可。

在 Brush 中选择这个**数据集目录**进行训练，而不是导入 `dense.ply` 或 `textured.obj`。这里的照片、位姿和稀疏点符合 [Brush 的 COLMAP 加载逻辑](https://github.com/ArthurBrussee/brush/blob/main/crates/brush-dataset/src/formats/colmap.rs)。训练完成后导出的 Gaussian PLY 是另一种结果，包含高斯参数，不能把普通点云当作已经训练的 3DGS。

脚本校验每张照片的实际尺寸、相机引用、有限且归一化的位姿、稀疏点坐标及轨迹中的图片引用。仅 PNG/JPEG 输入受支持。默认最大边长 1200 适合先在本机验证训练流程；它不代表训练效果已经得到保证。

数据集保留全部已注册视图，评估划分在 Brush 训练阶段配置。后续报告需固定训练/测试视图并在未参与训练的视图上评估，避免直接将训练视图的效果当作泛化结果。

### 本机 Brush 训练命令

已将下载的 Apple Silicon 压缩包解压到 `.tools/brush/`，实际程序名为 `brush_app`，已通过 `--help` 启动检查。这个发行包使用 **`--total-steps`**；与仓库 main 分支的 `--total-train-iters` 参数不同，以本机帮助为准。

先训练 5,000 步并打开实时查看窗口：

```bash
cd /Users/shenshen/Desktop/design3

./.tools/brush/brush_app ./data/brush/courtyard \
  --total-steps 5000 \
  --max-resolution 1200 \
  --max-splats 1000000 \
  --export-every 1000 \
  --export-path /Users/shenshen/Desktop/design3/outputs/brush-courtyard \
  --with-viewer
```

每 1,000 步导出一个 `export_{iter}.ply` 到指定目录。已有手动训练结果 `outputs/brush-courtyard/export_5000.ply`。后续更完整的训练可将步数改为 30,000 并使用新的输出目录；去掉 `--with-viewer` 则只在终端运行。程序参数可通过 `./.tools/brush/brush_app --help` 查看。

### 通过项目脚本调用 Brush

`recon_workbench/train_3dgs.py` 已接入 Brush CLI，自动查找 `.tools/brush/brush_app` 或 PATH 中的 Brush，并按本机 `--help` 适配 `--total-steps` / `--total-train-iters`。

复用已整理的数据集：

```bash
python3 -m recon_workbench.train_3dgs \
  --dataset data/brush/courtyard \
  --output outputs/3dgs-courtyard-5000 \
  --steps 5000 \
  --with-viewer
```

也可以直接复用 SfM，脚本自动整理输入后训练：

```bash
python3 -m recon_workbench.train_3dgs \
  --sfm-output outputs/20261008_234029_607758 \
  --output outputs/3dgs-courtyard-eval \
  --steps 30000 \
  --eval-split-every 8 \
  --with-viewer
```

两个输入选项互斥，输出目录必须不存在；省略 `--output` 自动创建时间戳目录。默认 5,000 步、分辨率最大边长 1200、最多 100 万个高斯、每 1,000 步导出、seed 42。`--eval-split-every 8` 在 Brush 排序后的视图中每 8 张留出一张，不参与高斯训练，评估结果在训练日志中。相机位姿仍由全体照片的 SfM 估计，这属于已知位姿下的视图留出评估。高斯增密和训练的资源消耗由 Brush 管理。

```bash
python3 -m recon_workbench.train_3dgs --check-env
python3 -m recon_workbench.train_3dgs --dataset data/brush/courtyard --dry-run
```

`--brush` 可指定可执行文件，`--colmap` 指定输入校验用 COLMAP。`--dry-run` 只做环境与基础路径检查并打印训练命令，不创建结果目录；实际训练前另用 COLMAP 导出模型，核对照片尺寸、位姿和点轨迹。当前要求使用 `recon_workbench/prepare_brush.py` 的标准数据集，拒绝额外的 `args.txt` / `init.ply` 覆盖项。

结果结构：

```text
outputs/3dgs-courtyard-5000/
├── gaussians/export_5000.ply  # 最终高斯模型及各次定期导出
├── config.json               # 参数、版本、实际训练命令
├── status.json               # running / completed / failed / cancelled
├── summary.json              # 成功后生成，含最终路径、高斯数、耗时
├── logs/03_train.log          # Brush 输出，包括评估和异常
└── dataset/                  # 仅 --sfm-output 模式生成
```

不指定 `--with-viewer` 时后台仅使用终端；日志可用 `tail -f 输出目录/logs/03_train.log` 查看。使用查看窗口时，应等待最后一步导出再关闭。Ctrl+C 会向训练子进程发送中断，并保留已导出模型和日志。脚本检查最终 Gaussian PLY 的属性与数据长度，只有最后一步导出完整才标记完成。

本机集成验证：`outputs/brush-integration-gpu-smoke/` 跑通自动整理 SfM → 输入校验 → Metal GPU 训练 → 最后一步导出；`outputs/brush-integration-terminal-smoke/` 另验证已整理输入及评估，31 张训练视图、5 张评估视图，完整导出 `export_10.ply`。仅 10 步、最大边长 320 的验证用于检查流程，不代表重建质量。macOS 下使用伪终端捕获 Brush 0.3 的进度和评估显示，日志包含终端控制字符。

相关回归检查：

```bash
python3 -m unittest tests.test_reconstruct tests.test_prepare_brush tests.test_train_3dgs
```

## 统一入口 main.py

用户通过 `--method sfm|mvs|3dgs` 选择路线，统一入口调用原有脚本，不复制算法实现。`--images` 从照片开始；`--sfm-output` 复用 SfM；`--dataset` 复用已整理的 Brush 数据集。三个输入选项互斥。

```bash
# ETH3D 已去畸变照片：仅 SfM
python3 main.py --method sfm \
  --images data/eth3d/courtyard/images --camera-model PINHOLE

# 从照片开始，生成点云、网格和纹理
python3 main.py --method mvs \
  --images data/eth3d/courtyard/images --camera-model PINHOLE

# 从照片开始，自动完成 SfM、整理和 Brush 训练
python3 main.py --method 3dgs \
  --images data/eth3d/courtyard/images --camera-model PINHOLE \
  --steps 5000 --with-viewer

# 复用已有数据集训练
python3 main.py --method 3dgs \
  --dataset data/brush/courtyard \
  --output outputs/unified-courtyard-3dgs --steps 5000 --with-viewer

# 复用 SfM；也可以将 --method 改为 mvs
python3 main.py --method 3dgs \
  --sfm-output outputs/20261008_234029_607758 --steps 5000
```

统一任务目录包含根级 `config.json` / `status.json` / `summary.json` / `logs/`，各阶段结果存放在 `sfm/`、`mvs/` 或 `3dgs/`。复用输入时跳过已经完成的阶段。默认自动创建时间戳目录，也可以显式 `--output` 指定不存在的新目录。失败保留已有结果，各阶段退出码记录在根级状态文件；Brush 详细日志在 `3dgs/logs/03_train.log`。

`--mvs-stage dense|mesh|textured` 控制传统路线最后一步，默认 textured；`--max-image-size` 同时传给各阶段的最大边长设置。ETH3D 已去畸变图片使用 PINHOLE，普通照片默认 SIMPLE_RADIAL。统一入口支持 Brush 的步数、高斯上限、导出周期、评估间隔和查看窗口参数。`--dry-run` 只打印调度命令，实际工具与模型校验交给执行阶段。

### Brush 调用代码在哪里

- `recon_workbench/train_3dgs.py::inspect_brush()`：查找程序，运行帮助与版本检查，适配步数参数名称。
- `recon_workbench/train_3dgs.py::training_arguments()`：将项目参数转为 Brush CLI 参数列表。
- `recon_workbench/train_3dgs.py::main()`：创建任务目录、必要时整理数据、校验输入，然后调用 `pipeline.run("03_train", ...)`。
- `recon_workbench/reconstruct.py::Pipeline.run()`：将程序路径与参数组成列表，使用 `subprocess.Popen` 启动子进程，保存日志并处理退出码和中断。训练使用 Brush 可执行文件，不会调用 COLMAP；输入校验阶段显式指定 COLMAP。
- `recon_workbench/pipeline.py::build_plan()`：选择路线，构造各阶段命令；`recon_workbench/pipeline.py::main()` 通过同一执行器依次运行 Python 脚本并汇总结果。

```text
main.py --method 3dgs
  ├─ recon_workbench/reconstruct.py（--images 模式）→ COLMAP SfM
  └─ recon_workbench/train_3dgs.py
       ├─ recon_workbench/prepare_brush.py（--sfm-output 模式）→ COLMAP 去畸变与整理
       ├─ COLMAP model_converter → 输入校验
       └─ Pipeline.run → subprocess.Popen → brush_app → Gaussian PLY
```

回归检查：`python3 -m unittest tests.test_reconstruct tests.test_prepare_brush tests.test_train_3dgs tests.test_main`。

## 本地界面与评估对比

激活项目虚拟环境、安装 `requirements.txt` 后启动界面：

```bash
python3 app.py
```

浏览器打开 `http://127.0.0.1:8765`；`--port` 可更换端口，`--no-browser` 只启动服务。服务仅监听本机回环地址，无需账号。在终端 Ctrl+C 会关闭服务，并向当前界面启动的任务发送中断。

界面支持：

- 上传 JPG/PNG 照片并查看缩略图，自动设为重建输入；也可浏览已有照片、SfM 结果或 Brush 数据集目录。
- 配置训练步数、图像尺寸、相机模型、评估划分、随机种子和导出周期。
- 启动一个任务、查看状态和日志、停止任务；每次自动创建新的输出目录。
- 选择已完成实验导出 JSON/CSV；查看评估曲线和渲染图片，打开结果目录或在 Brush 中查看高斯模型。

界面的目录浏览范围为本项目，其他位置的照片可通过上传选择；CLI 仍支持其他绝对路径。上传文件复制到新的 `data/uploads/批次/` 目录，支持最多 500 张、单张 64 MB、单批 1 GB，不覆盖原照片。上传后输入类型自动改为原始照片，普通照片默认 SIMPLE_RADIAL；ETH3D 去畸变照片可手动改为 PINHOLE。

点击已完成任务名称，或等待新任务完成，页面会加载实际 PLY 点云，可拖动旋转、滚轮缩放、Shift 拖动平移、改变点大小。稀疏/稠密点云可切换，普通点云使用本地 WebGL 渲染，无外部 CDN 依赖。预览最多显示 30 万个均匀抽样点，原文件保留全部点；默认按中心 98% 坐标范围聚焦主体，避免离群点使主体过小。该视角设置不修改几何数据。网格表面和 3DGS 高斯渲染仍使用 MeshLab / Brush；不将 Gaussian PLY 当作普通点云渲染。

首次使用已有 ETH3D 数据可选 `3dgs`、`已整理的 Brush 数据集`、`data/brush/courtyard`，5,000 步、最大边长 1200、每 8 张留出一张，并保存评估图片。完整的上传→重建→内嵌查看可选择 SfM 或 MVS 路线。

### 指标与实验协议

`recon_workbench/evaluate.py` 汇总已有结果；不重新执行重建，也不把缺失值填为 0。

| 路线 | 已实现指标 | 当前限制 |
| --- | --- | --- |
| SfM | 注册率、平均点重投影误差、稀疏点数 | 重投影误差不能直接当作真实几何精度 |
| MVS | SfM 指标、稠密点数、网格面数 | 尚未接入对齐的几何真值，不报告准确度/完整度 |
| 3DGS | 留出视图 PSNR/SSIM、随步数的评估曲线、高斯数 | 全训练视图实验不产生有效的留出评估 |
| 通用 | 任务范围内耗时、结果文件大小、配置和来源 | 复用输入耗时不包含以前的 SfM；未测峰值内存和 FPS |

3DGS 训练新增 `split.json` 和 `evaluation.json`。按照 Brush 0.3.0 的规则，按文件名排序后，索引 0、8、16……作为每 8 张留一张的评估视图。`split.json` 保存实际视图名单、分辨率、seed 和相机模型文件指纹；`evaluation.json` 保存从 Brush 日志解析并去重的逐步 PSNR/SSIM。末次指标只对应记录的评估步数，不强行当作最终模型指标。

这是**已知相机位姿条件下的视图留出评估**：相机和初始化稀疏点仍来自全体照片的 SfM，评估视图不参与高斯优化；不是从整个前处理链中隔离的测试集。不同算法若要比较新视图质量，还需使用相同测试相机和渲染协议。

同一 `comparison_group` 记录相同位姿文件、评估视图、分辨率和 Brush 版本，可用于固定其他参数后研究训练步数等因素。该分组不会自动控制所有参数，仍需检查完整配置。同场景不同路线可以对照时间、文件大小与可视化，不能直接用普通点数和高斯数排名，MVS 尚没有对应的 PSNR/SSIM 渲染评估。

```bash
# 训练并保存留出视图的渲染
python3 main.py --method 3dgs \
  --dataset data/brush/courtyard \
  --steps 5000 --eval-split-every 8 --save-eval-images \
  --output outputs/courtyard-evaluated

# 汇总所有顶层实验，报告写入新时间戳目录
python3 -m recon_workbench.evaluate --scan outputs

# 只对比指定实验
python3 -m recon_workbench.evaluate --runs outputs/实验A outputs/实验B \
  --output outputs/reports/新的对比目录
```

手动 Brush 结果（如 `outputs/brush-courtyard/export_5000.ply`）也会出现在列表中，只读取最后导出的高斯数和文件大小；没有配置、耗时和留出记录的字段保持空白。CSV 使用 UTF-8 BOM，可在 Excel 中打开。报告中的文件大小统计列出的结果文件，未包含数据集、全部检查点和中间深度图。

### 界面实测

已在真实浏览器中通过文件选择器上传 20 张样例照片，并完成 SfM 重建，在页面内加载 3,608 个稀疏点；另验证现有 ETH3D 290,739 点的彩色稠密点云、拖动旋转及主体视角聚焦。对应上传重建结果保存在 `outputs/uploaded-sfm-check_20261009_175344_995892/`。上传、PLY 解析与抽样检查已加入回归测试。

已通过真实浏览器启动 10 步、最大边长 320 的短训练，核对任务完成状态、31 个训练视图和 5 个评估视图、PSNR/SSIM、5 张保存的评估渲染图片，以及界面报告导出。这仅验证工程流程，正式质量比较需另跑足够步数的固定协议实验。已有汇总报告：`outputs/reports/initial-comparison/`。

全部检查：

```bash
python3 -m unittest tests.test_reconstruct tests.test_prepare_brush tests.test_train_3dgs tests.test_main tests.test_evaluate tests.test_app tests.test_pointcloud
```

## 项目与历史管理

启动界面：

```bash
python3 app.py
```

左侧“项目管理”可以创建、切换和改名项目。上传的照片、保存的输入目录、任务和模型按项目关联；选择“复用当前项目的图片集 / 输入”可再次使用已有照片、Brush 数据集或 SfM 结果。项目详情显示全部状态的任务，点击名称可查看参数、调用命令、阶段与日志；已完成任务可以继续打开原有结果查看界面。实验对比只展示当前项目的成功结果。

元数据存储在 `data/workbench.sqlite3`，SQLite 由 Python 自带；图片检测所需 Pillow 通过 `requirements.txt` 安装。图片、点云与模型继续保留在文件夹中，数据库和资产不提交到 GitHub。备份本地工作时需要同时保存数据库、`data/` 和 `outputs/`；组员首次启动会建立自己的数据库。

已有实验启动时自动登记到“默认项目 / 历史实验”，原始文件不改写。旧记录优先采用状态文件的开始时间，缺失时使用文件夹修改时间。CLI 新结果在下次服务启动时登记。任务完成状态由后台线程保存，不依赖页面是否打开。正常 Ctrl+C 会停止任务；异常退出后再启动，未正常结束的任务标记“已中断”，不会自动续跑或控制旧 PID。

同一项目目录只能启动一个服务。这里仍是本地单用户工作台，账户、并行队列、网页网格/高斯渲染和模型下载属于后续任务。

验证命令：

```bash
python3 -m unittest
```

`tests/test_workspace.py` 覆盖图片集保存、重启恢复、历史导入不重复、项目关联校验、任务启动失败、后台完成与取消，以及本地 HTTP 上传和失败任务的重启验证。测试使用临时数据和轻量子进程，不代表真实算法质量评估。接口字段见 [COLLABORATION.md](COLLABORATION.md)。

## 图片质量检测与筛选

网页从原始照片开始时，先选择图片集（或填写目录），点击“检测当前图片集”，确认勾选并“保存筛选”，再开始重建。模糊与曝光提示不自动取消勾选；完全重复按 SHA-256 识别，可点击按钮排除额外副本。无法解码的文件不能参与重建。疑似相似仅提示，避免把有用的相邻视角误删。

检测使用最长边最多 512 像素的灰度图：拉普拉斯方差默认低于 60 提示模糊/少纹理；亮度均值默认低于 40 或高于 215，以及暗/亮截断像素比例达到 60% 时提示曝光异常。相似提示使用 64 位 dHash（默认汉明距离不超过 4），同时限制宽高比和亮度差，并跳过低对比度图片。阈值可在页面调整；这些是启发式提示，没有经过通用准确率验证。

报告、阈值、内容指纹和筛选名单保存在数据库，重启后可恢复。上传新文件、重新检测后内容或阈值变化，会使旧确认失效。原始照片不会改写；每次网页任务复制选中照片到 `data/task_inputs/任务目录名/images/`，并保存 `selection.json`（实际文件名单和 SHA-256）与 `quality.json`。启动前发现选中文件变化会阻止执行并记录失败；这些副本也必须随本地实验一起备份。已有 SfM / Brush 输入不在此筛选，CLI 原有调用方式保持不变。

完整手动步骤、样例生成方式和实测记录见 [TESTING_QUALITY.md](TESTING_QUALITY.md)。解码及 EXIF 方向处理采用 [Pillow Image](https://pillow.readthedocs.io/en/stable/reference/Image.html) 和 [ImageOps](https://pillow.readthedocs.io/en/stable/reference/ImageOps.html)。

## 参考

- [COLMAP 命令行流程](https://colmap.github.io/cli.html)
- [COLMAP 安装](https://colmap.github.io/install.html)
- [COLMAP 模型格式](https://colmap.github.io/format.html)

实现说明：本项目负责调用、参数适配、任务状态、产物管理和统计；SfM 算法由 COLMAP 执行。

- [OpenMVS 与 COLMAP 接口及完整流程](https://github.com/cdcseacave/openMVS/wiki/Usage)
