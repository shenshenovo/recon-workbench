# 数据集选择与分层测试计划

这是测试计划，不是重建性能结果。2026-10-19 已下载并验证三个场景，准备了首轮图片子集；路径和实际数量见第 7 节，尚未运行本计划的大规模重建。

## 1. 三个有明确用途的真实场景

选同一公开数据集的三个场景即可，不要求来自三个不同机构。以下都出现在 Nerfstudio 官方下载脚本中，兼顾物体、室内与建筑。应用意义是本项目的测试定位，不表示数据作者以这些行业应用为采集目的。

| 场景 | 首轮目标输入数 | 测试定位与应用意义 |
| --- | ---: | --- |
| `dozer` | 50 | 工程机械物体：数字资产展示，观察局部细节、遮挡和环绕视角的重建 |
| `kitchen` | 100 | 室内空间：空间记录、室内浏览，观察纹理较少的区域、遮挡和反光 |
| `Giannini-Hall` | 200 | 建筑：校园建筑数字化展示，观察较大场景覆盖和重复结构 |

50/100/200 是建议抽取的子集规模，**不是官方原始张数**。下载后先统计唯一视角数、尺寸和文件大小；不足时采用实际数量，禁止复制照片凑数。ETH3D courtyard 继续保留为已跑通的回归基线。

三场景用于检验适用性；它们的耗时差异同时受到纹理、视角、镜头等影响，不能仅据此推断图片数量的性能关系。

## 2. 数据来源和下载

- [老师提供的官方数据集目录](https://github.com/nerfstudio-project/nerfstudio/blob/main/docs/quickstart/existing_dataset.md)
- [可阅读的官方文档](https://docs.nerf.studio/quickstart/existing_dataset.html)
- [当前官方下载脚本与逐场景链接](https://github.com/nerfstudio-project/nerfstudio/blob/main/nerfstudio/scripts/downloads/download_data.py)
- [Nerfstudio 论文，场景与采集方式见第 6 节及附录表 4](https://arxiv.org/html/2302.04264v3)

官方脚本列出的单场景下载入口：

| 场景 | 下载入口 |
| --- | --- |
| dozer | [Google Drive](https://drive.google.com/file/d/1jQJPz5PhzTH--LOcCxvfzV_SDLEp1de3/view) |
| kitchen | [Google Drive](https://drive.google.com/file/d/1IRmNyNZSNFidyj93Tt5DtaEU9h6eJdi1/view) |
| Giannini-Hall | [Google Drive](https://drive.google.com/file/d/1UkjWXLN4qybq_a-j81FsTKghiXw39O8E/view) |

上述入口来自官方脚本，但 2026-10-19 实际请求均未能下载，返回 404。本次改用 [NerfBaselines 公开镜像](https://huggingface.co/datasets/nerfbaselines/nerfbaselines-data/tree/main/nerfstudio)，固定版本与包哈希记录在本机下载清单中；不是另换了场景。先下载单场景，不使用 `all`。

如果已有独立 Nerfstudio 环境，可使用官方 CLI；它依赖上述 Google Drive 源，当前源失效时这些命令也可能失败：

```bash
ns-download-data nerfstudio --capture-name dozer
ns-download-data nerfstudio --capture-name kitchen
ns-download-data nerfstudio --capture-name Giannini-Hall
```

本工作台不需要安装整个 Nerfstudio 来使用照片；可通过浏览器下载并解压到 `data/datasets/nerfstudio/`。记录下载日期、来源、数据许可、原始图片名单与 SHA-256。

## 3. 导入时先区分照片和已处理的相机数据

- 检查目录结构，只选择一套 RGB 图片。`images`、`images_2`、`images_4` 常是同一批视角的不同分辨率，不能合在一个输入里；深度、掩码也不混入 RGB 输入。
- 首轮测试本项目完整流程：选 JPG/PNG 照片，质量检测、保存选择，再重新运行 COLMAP。不要直接复用发布数据的相机结果，却将该次计时写成完整 SfM 耗时。
- 官方论文表 4 标注 dozer 和 Giannini Hall 为鱼眼采集，kitchen 为手机采集且原位姿来自 Polycam。核对实际包内相机模型和图片是否去畸变；不能一律选 `PINHOLE`。原始鱼眼图可先用项目支持的 `OPENCV_FISHEYE` 试验；已去畸变图使用对应的针孔模型。
- 本项目当前“已有 Brush 数据集”入口检查本项目整理的 `dataset.json` 和 COLMAP 目录，**不能保证把任意 Nerfstudio `transforms.json` 文件夹直接作为该入口使用**。完整流程跑通后，再单独评估已有相机数据的适配。
- 若只抽照片进行重新 SfM，可以单独生成图片子集；若复用发布相机，必须同步筛选相机记录，缩放图像也必须同步内参。

## 4. 图片数量与工作台容量

从有足够唯一视角的同一场景构建 50、100、200、300、400、500 张子集，数据不足的档位跳过。各子集尽量覆盖同一拍摄范围，小集合包含于大集合；按轨迹均匀抽样并保存名单，避免只取前 N 张造成覆盖变化。稀疏抽样可能降低相邻重叠，失败时单独分析几何覆盖。

当前代码的限制：

| 环节 | 当前设定 | 测试含义 |
| --- | --- | --- |
| 网页上传 | 单批最多 500 张、单张 64 MiB、单批 1 GiB | 501 张或超大小是边界拦截，不是机器性能失败 |
| 图片检测 | 最多 500 张，单图最多 4000 万像素、读取不超过 64 MiB | 本地目录输入也受检测数量限制 |
| 任务调度 | 同时一个任务 | 本轮测试单任务容量，不宣称多用户并发能力 |
| 网页普通点云 | 默认抽样显示至多约 30 万点 | 区分模型原始点数和网页实际绘制点数 |

限制依据：`recon_workbench/server.py`、`image_quality.py`、`pointcloud.py`。CLI 可以绕过网页质量筛选，但其结果不代表网页工作流支持了更大输入。

分开测四层：上传/图片列表、质量检测、三维计算、结果读取与交互。先跑 SfM 数量阶梯，再在已通过的档位测试 MVS/3DGS，不必立即将所有组合跑满。

初始固定条件建议：最大边长 1200、CPU 4 线程、SfM exhaustive、MVS dense、3DGS 5000 步且固定高斯上限与评估拆分。实际镜头模型按场景选择；同场景数量阶梯保持一致。exhaustive 的图片对数从 50 张的 1225 对增长到 500 张的 124750 对；更换 sequential 要单独记录为另一组配置。

提前确定本机的可接受耗时与资源预算。例如单任务超过 30 分钟人工取消，记作“在该预算内未完成”，不能断言算法不能完成。内存压力持续升高、系统失去响应时结束测试并保留日志。峰值内存应覆盖 COLMAP/OpenMVS/Brush 子进程；只看 `app.py` 内存会低估开销。CPU 和 Metal 训练分别记录资源口径。

每档先跑一次探索，再对基线与临界档位各复测至少三次，记录中位数和范围。最终报告“指定电脑、版本和配置下，最大已验证输入为 N 张”；未触及失败边界时只能报告下界。人工停止、内存不足、几何注册失败、上传拦截、网页展示问题分别记录。

## 5. 异常图片测试集

不依赖网络上碰巧损坏的文件。对正常集的副本注入已知异常，保存来源、变换参数、随机种子和逐文件标签，原始数据保持不变。

| 集合 | 构造 | 通过条件 |
| --- | --- | --- |
| 文件损坏 | 空文件、伪装扩展名、保留头部但截断像素数据的 JPEG/PNG | 上传阶段拒绝或检测阶段报告不可解码；不进入最终输入，其他正常图片仍可处理 |
| 视觉退化 | 不同程度的模糊、欠曝、过曝，每次仅改一种因素 | 记录分数变化、提示、漏报和正常图误报；轻微退化不强制要求报警 |
| 重复图片 | 不同文件名的逐字节副本；另做 JPEG 重编码版本 | 完全重复按 SHA-256 判断；重编码只作为相似候选，不自动删除正常邻近视角 |
| 混合异常 | 在固定正常集上分别加入约 10%、20% 的异常副本 | 页面可用、异常来源可追踪、保存数量准确，启动只复制已确认名单 |

10%/20% 定义为最终集合中文件占比，报告写清分母；大小不超过当前输入限制。损坏、曝光、模糊等标签允许同时存在。完全重复用组或图片对统计，避免将原图当作必须删除的错误。

现有脚本可立即进行小样例验收，三个场景各运行一次（把 `images` 换成解压后的实际 RGB 目录）：

```bash
python -m scripts.make_quality_demo --source data/datasets/nerfstudio/dozer/images --output data/quality/dozer-smoke
python -m scripts.make_quality_demo --source data/datasets/nerfstudio/kitchen/images --output data/quality/kitchen-smoke
python -m scripts.make_quality_demo --source data/datasets/nerfstudio/Giannini-Hall/images --output data/quality/hall-smoke
```

现有脚本只取最多前 8 张正常照片并增加 6 个样例，生成 `demo.json` 记录对应关系；不能将它描述成上述完整比例/多等级测试集。目录不可重复使用，不上传 JSON。`zz_broken.png` 可经过基础头部检查，再由完整解码检查发现问题。其他明显无效文件可能在上传时就被拦截，这也应记为正确处理。

完整比例测试集需另行生成；质量检测准确率还需要人工标注真实正常图与问题图，以图片来源划分标定集和验证集，避免同一原图的不同变体跨集合。阈值先标定再固定，分别报告各类 precision/recall、误报和漏报，不用告警总数作为准确率。

筛选前后重建对照使用“可解码的视觉退化/重复图”；不可解码文件单独测试异常处理。保持原有正常视角，比较干净基线、加入退化图、人工确认筛选后三组。不得预设筛选一定提升速度或模型效果。

## 6. 每次实验保存的证据

记录场景来源、输入文件清单与指纹、唯一视角数/文件数、文件总大小、分辨率、镜头模型、路线、工具版本、参数、最大高斯数、评估名单、计时范围、任务 ID 和日志。

- 工作台：上传/检测耗时、成功启动与结束、失败原因、结果首次显示时间，点云原始/绘制点数和交互情况。
- SfM：输入数、注册数/注册率、重投影误差。点多或误差小不单独证明几何精度。
- MVS：耗时、产物大小、点数和可视覆盖；取得适用几何真值并对齐前，不报告精度/完整度指标。
- 3DGS：固定留出视角的 PSNR/SSIM、截图、步数和耗时。与不同路线的几何指标不合成总分。复用相机数据和从照片开始的端到端任务分开计时。

推荐次序：下载并核对三个场景 → 每场景小样例导入 → 50/100/200 张适用性测试 → 同场景数量阶梯 → 三场景异常样例 → 更系统的标注与筛选对照。未执行的格子保留空白，不用自动化单元测试数量代替真实实验数量。

## 7. 本机已下载与可直接使用的路径

2026-10-19 从 NerfBaselines 镜像下载，固定版本 `6a638951a007c8b47eb8a0301d48b04d18e955cf`。三个 ZIP 的字节数和 SHA-256 均与镜像发布值一致，解压时检查 ZIP CRC；`transforms.json` 引用的 1434 张 RGB 图片均存在且完整解码成功。这只证明下载完整和图片可读，不证明图片清晰、适合重建或已通过质量筛选。

| 场景 | 完整 RGB 数量 | 分辨率 | 包内相机模型 | 完整照片路径 |
| --- | ---: | --- | --- | --- |
| dozer | 359 | 3008 × 2000 | OPENCV_FISHEYE | `data/datasets/nerfstudio/dozer/images` |
| kitchen | 617 | 994 × 738 | OPENCV | `data/datasets/nerfstudio/kitchen/images` |
| Giannini-Hall | 458 | 3008 × 2000 | OPENCV_FISHEYE | `data/datasets/nerfstudio/Giannini-Hall/images` |

完整包保留了发布者的其他分辨率、相机和附带文件；工作台选择上表的 `images`，不要选择整个场景根目录递归混入其他副本。

首轮子集是原 RGB 的逐字节副本，没有重新编码、缩放或去畸变，也没有将发布相机结果当成本项目 SfM 输出：

| 首轮输入 | 照片路径 |
| --- | --- |
| dozer 50 张 | `data/benchmarks/nerfstudio/dozer_050/images` |
| kitchen 100 张 | `data/benchmarks/nerfstudio/kitchen_100/images` |
| 建筑 200 张 | `data/benchmarks/nerfstudio/Giannini-Hall_200/images` |

按原文件名排序，通过不断二分最大索引间隔选择照片，包含首尾视角；同一选择序列的前缀可用于以后构建嵌套阶梯。该方法只改善索引覆盖，不能保证相机空间均匀或重叠充足。每组 `subset.json` 保存来源路径、源索引、文件大小和 SHA-256；没有为子集提供可直接复用的相机位姿。

网页操作：打开重建工作台 → 输入类型选原始照片 → 输入上述子集的 `images` 路径 → 检测、勾选并保存 → 下一步选 SfM。dozer 和建筑首轮按包内模型选 `OPENCV_FISHEYE`，还需提供第 8 节的已知内参；kitchen 选 `OPENCV`。最大边长先用 1200、线程 4。它们的原始分辨率不同，因此三场景耗时不是严格的单变量数量对照。

kitchen 的 617 张超过当前检测上限，先用 100 张子集；后续可在同一 kitchen 场景内构建直到 500 张的数量阶梯。dozer、建筑的完整视角数不足 500，不能用复制文件补足来声称支持更多视角。

本机清单与证据（均在忽略的 `data/` 中）：

- `data/datasets/nerfstudio/_downloads/download-manifest.json`：镜像版本、三个包的来源、字节数和 SHA-256。
- `data/datasets/nerfstudio/inventory.json`：完整 RGB 数量、尺寸、解码结果。
- 每场景 `download-manifest.json`：逐张 RGB 的 SHA-256、尺寸和大小。
- `data/datasets/nerfstudio/LICENSE.nerfstudio.txt`：镜像随附的许可证原文。
- `data/benchmarks/nerfstudio/inventory.json` 与各组 `subset.json`：首轮输入与源文件映射。

下载和子集文件不会提交 GitHub，组员需要取得相同版本或按清单复现。这一步尚未生成损坏图片集，亦未给出新的重建精度或容量结论。后续 dozer 的单次流程复验见第 8 节。

## 8. 鱼眼焦距先验与 dozer 50 张复验

最初仅选 `OPENCV_FISHEYE`，未提供已知内参，dozer 50 张任务在 mapper 失败。检查原数据库：50 张都有特征、1218 对有初始描述子匹配，但几何验证通过的图像对为 0；50 个相机均 `prior_focal_length=0`，焦距使用默认值 3609.6。匹配日志明确提示当前 COLMAP 将无焦距先验的鱼眼图像对标记为退化，无法从基础矩阵恢复焦距。这不是文件损坏，也不是 50 张图片的容量上限。

现在网页提供“已知相机内参”和“所有照片共享内参”，统一 CLI 与单独 SfM CLI 支持 `--camera-params`。先选择相机模型，再填写内参；切换来源或模型会清空旧内参。相同镜头、焦距和尺寸的本场景照片可以勾选共享内参。**下列值直接来自各自发布包的 `transforms.json`，对应 3008×2000 原图，不能按特征提取最大边长 1200 缩放。**

dozer，顺序 `fx,fy,cx,cy,k1,k2,k3,k4`：

```text
1072.281897246229,1068.6906965388932,1504,1000,0.03126218448029553,0.005177020067511987,0.0006640977794272005,0.00010067035656515042
```

Giannini-Hall（只核对来源，尚未使用这 200 张完成重建验证）：

```text
1072.6630410906932,1069.4657942131244,1504,1000,0.030940324152232777,0.00595873433839829,0.00032131261265316293,0.00011198094930207011
```

dozer 的 CLI 示例（输出目录必须不存在；重复运行换新名称）：

```bash
python main.py --method sfm \
  --images data/benchmarks/nerfstudio/dozer_050/images \
  --output outputs/dozer50-camera-prior \
  --camera-model OPENCV_FISHEYE \
  --camera-params '1072.281897246229,1068.6906965388932,1504,1000,0.03126218448029553,0.005177020067511987,0.0006640977794272005,0.00010067035656515042' \
  --single-camera --max-image-size 1200 --threads 4
```

2026-10-19，用原失败任务的同一批 50 张输入快照完整重跑特征提取、匹配、建图和导出；使用发布内参初始化并共享相机，没有导入发布位姿或三维点。数据库中焦距先验标记为 1，837 对通过几何验证，最终注册 50/50 张、生成 21995 个稀疏点，平均点重投影误差约 0.787 px，SfM 脚本耗时约 73.3 秒、统一路线约 73.8 秒。这是一次流程复验，不是容量极限或几何真值精度结论。

结果：`outputs/dozer-fisheye-prior-check_20261019_173307/`。已登记到原项目，任务名 `dozer-50-fisheye-prior`，任务 ID `6421e85426d94f98a83f724c30141b62`；原失败任务与日志保留。重启网页服务并刷新后可使用新增内参字段，历史结果也可从项目记录直接查看。

标定参数的用法参见 [COLMAP 官方 FAQ：Set known camera intrinsics](https://colmap.github.io/faq.html)。COLMAP 默认仍会在 bundle adjustment 中优化内参，当前接口没有把这些参数锁定。
