# 图片质量检测与筛选：验收与实测

本文件位于 `docs/`，以下命令都在仓库根目录执行；返回 [项目首页](../README.md)。任务要求与交付对应关系见 [图片质量任务验收清单](QUALITY_DELIVERY.md)。

当前全面复查、两项修复的回归与浏览器验证见 [复查记录](QUALITY_RECHECK.md)。已完成的实验结论仍受各自数据、标签和执行版本约束。

本阶段验证“上传 / 选择照片 → 检测 → 人工筛选 → 保存 → 使用选中图片重建”，完成后可先暂停新增功能。这里的模糊、曝光和相似提示是启发式规则，不是经过标定的分类准确率；真实三维算法仍由 COLMAP / OpenMVS / Brush 执行。

## 1. 准备环境

在项目根目录执行。建议使用 Python 3.11+：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest
```

已存在 `.venv` 时只需激活并安装依赖。所有 Python 测试使用临时文件；HTTP 测试需要允许本地端口监听，不需要 GPU、COLMAP 或数据集。2026-11-05 对提交内容的独立副本运行全量 Python 回归，150/150 通过；V2 前端回归另运行 `node tests/test_quality_ui.js`，结果为 10/10 通过。ImageHash 会把 NumPy、SciPy 作为传递依赖安装，不必为基础测试安装 CUDA。`tests/test_image_quality.py` 验证检测、任务输入及旧算法报告重新确认，`tests/test_image_similarity.py` 覆盖默认 2% 边界及边界外，`tests/test_workspace.py` 验证保存、重启和 HTTP 流程；其他质量测试的对应关系见验收清单。直接运行重建 CLI 不会强制调用此检测模块。

## 2. 启动并快速查看

```bash
python app.py
```

在“重建工作台”的“图片与筛选”步骤创建项目，选择至少两张照片并“上传图片集”，或在新建任务中选择“原始照片”并填写项目内目录。点击“检测当前图片集”，查看每张图片的清晰度、亮度、暗/亮像素比例及问题提示。低清晰度分数可能来自模糊，也可能来自少纹理；相似照片可能正是重建所需的相邻视角，人工决定是否使用。

默认全选可解码图片，但不自动保存。可勾选、取消，或点击“排除完全重复”保留每个已选重复组的一个副本；无法解码的行不能选中。点击“保存筛选”后才能从网页开始原始照片重建。已有 SfM / Brush 输入不在这里筛选。

## 3. 生成有预期结果的样例

有本机 ETH3D 数据时，可用真实照片的副本生成样例：

```bash
python -m scripts.make_quality_demo \
  --source data/brush/courtyard/images \
  --output data/quality-demo
```

没有数据集时，生成合成纹理用于测试检测：

```bash
python -m scripts.make_quality_demo --output data/quality-demo-synthetic
```

脚本拒绝覆盖已有目录，再生成时换一个 `--output`。真实照片目录最多复制前 8 张为 `view_00`～`view_07`，再生成 6 个问题样例；合成模式使用 2 张纹理图，再生成同样的 6 个样例。来源图片不修改。`demo.json` 记录样例对应关系。

在网页上传生成目录里的 JPG/PNG，或将该目录作为原始照片输入；不要上传 `demo.json`。检测后重点观察：

| 样例 | 应观察到什么 |
| --- | --- |
| `zz_blur.jpg` | 清晰度明显降低，通常提示疑似模糊 |
| `zz_dark.jpg` | 亮度很低，提示疑似欠曝 |
| `zz_bright.jpg` | 亮度很高，提示疑似过曝 |
| `zz_exact_copy.*` | 与对应 `view_00.*` 的 SHA-256 相同，提示文件完全重复 |
| `zz_similar.jpg` | 压缩版本，可能提示疑似相似，不保证对所有场景都匹配 |
| `zz_broken.png` | 可经过基础文件头检查，但完整解码失败，不能勾选 |

一张图片可以有多个提示。除了完全重复的文件哈希和人为损坏样例，不应把所有场景都得到固定数量的告警作为通过条件。

## 4. 手动验收清单

| 操作 | 预期 |
| --- | --- |
| 未检测或未保存就点“开始重建” | 提示先检测并保存，HTTP 同样拒绝绕过 |
| 检测后取消全选，再保存 | 提示至少选择两张，不启动任务 |
| 只选择一个重复组中的副本，再点“排除完全重复” | 保留这个已选副本，不强制补选原文件 |
| 勾选照片并保存 | 显示已保存数量；原图不删除 |
| 14 张中勾选 8 张并保存 | 输入下拉框与图片集列表显示“已选 8 / 总计 14 张”，下方说明本次使用 8 张；定时刷新及页面刷新后保持一致 |
| 修改勾选但不保存 | 页面阻止启动，避免使用旧确认造成误解 |
| 修改检测阈值 | 要求重新检测，随后重新确认筛选 |
| 检测或保存响应尚未返回 | 五项阈值、图片及配对勾选、批量选择按钮均禁用；切换显示筛选后仍禁用 |
| 检测或保存成功/失败 | 请求锁定解除；仍须重检时保存按钮继续禁用，无法解码图片仍不可选 |
| 请求期间切换图片集 | 旧响应不覆盖新图片集的结果或提示 |
| 打开使用旧宽高比规则的已保存报告 | 显示历史结果并要求重新检测；重新检测后仍须明确保存才能启动 |
| 刷新页面，重新选择保存的图片集 | 恢复报告与已保存勾选 |
| 在启动服务的终端 Ctrl+C，重新启动并选择图片集 | 报告、勾选和任务历史保留 |
| 已保存后修改某张选中原图，再启动 | 哈希校验失败，记录失败任务；重新检测和保存后再启动 |
| 使用合成样例 | 只验证检测、提示、保存与输入校验，不用于评价重建 |

实际重建验收请使用真实照片：在真实样例中只选 `view_*`，保存，点击“下一步：重建设置”，选择 SfM、最大边长 640、线程 2。ETH3D 已去畸变照片选 PINHOLE，普通相机照片通常用 SIMPLE_RADIAL。合成样例或少量不重叠的照片即使可解码，也可能无法重建，这不代表质量检测失效。

任务完成后，从项目的“全部任务历史”打开详情，检查：

- `parameters.selected_images` 与勾选名单一致。
- `parameters.source_input` 是来源目录；`parameters.input` 和实际命令 `--images` 指向 `data/task_inputs/任务目录名/images/`。
- `parameters.quality_manifest` 指向 `selection.json`，记录实际输入文件名、来源和 SHA-256；同级 `quality.json` 保留当次报告、阈值、算法版本与 Pillow、ImageHash 及其运行时依赖版本。
- 副本目录只包含选中的文件，原图目录仍完整。SfM 统计中的输入照片数与选中数量一致；成功注册数量另由场景和算法决定。

## 5. 已完成的本机验收

2026-10-18，Apple M4，本地 Pillow 13.0.0，通过独立浏览器实际上传基于 ETH3D courtyard 的 14 张样例：13 张可解码、1 张解码失败；提示模糊 3、欠曝 1、过曝 1、文件完全重复 1、疑似相似 2。提示数量可重叠，前述数值仅对应此次样例与默认参数。

验证了启动前确认、取消全选拦截、筛选刷新后恢复，以及 430 像素宽页面没有整体横向溢出。只选 8 张 `view_*`，CPU 2 线程、最大边长 640、PINHOLE，真实 SfM 成功注册 8/8 张，初次生成 2,772 个稀疏点，最终复核生成 2,773 个稀疏点；通过条件是输入名单一致和流程成功，不要求每次点数完全相同。该结果用于证明流程和筛选输入正确，不用于宣称检测提高了精度或速度。

本机结果保留在：

```text
项目：质量检测验收
任务：quality-ui-sfm-8-final
outputs/quality-ui-sfm-8-final_20261018_185822_661421/
data/task_inputs/quality-ui-sfm-8-final_20261018_185822_661421/
```

以上文件、样例、数据库与外部工具不在 Git 仓库中，其他电脑需自行生成。检验任务的最终命令和日志，比复用本机绝对路径更可靠。

## 6. 记录你自己的验收结果

保存一次页面截图和任务参数，记录数据来源、选中数量、异常提示、是否完成、输入数与注册数，以及结果目录。记录时区分流程可运行、检测评估和重建对照：前面的早期验收主要证明人工筛选与任务输入可追溯；后续真实照片对评估和筛选前后实验见 8.5～8.7，其结论范围应随样本、复核方式和统计口径一起说明。项目没有自研 SfM/MVS/3DGS 算法。

## 7. Fast3R 接口验证（2026-10-28）

含可选 NumPy 的环境通过 77 项测试；新增检查覆盖数量保护、独立路线分发、模型配置覆盖、点图与颜色对齐、置信度过滤、PLY 导出和历史任务持久化。HTTP 流程使用明确标记的模拟子进程，不下载模型。缺少 PyTorch/CUDA 时的真实 CLI 失败路径会保留错误日志，不生成点云。

独立浏览器验证了 Fast3R 参数显示、超过输入上限的拒绝提示、成功后提示清理、模拟点云展示、记录页回看，以及 430 像素页面布局。上述检查不证明 Fast3R 模型已在 RTX 4060 上运行；真实 GPU 验收步骤见 [FAST3R.md](FAST3R.md)。

验收记录应包含实际输入、工具版本、参数、日志与结果，区分流程检查、质量检测验证和重建质量评估，便于组员复现并定位差异。

## 8. 图片质量 V2 实测结果

本节汇总 2026-11-03～04 冻结执行版本的流程、速度、真实视角配对评估及筛选前后 SfM 结果。8.1～8.2 为流程和规模检查，8.5 为真实视角复核评估，8.6～8.7 为两类独立筛选实验；各项结论应与其数据、复核方式和统计口径一起使用。2026-11-05 修复后的旧/新配对判定回放无差异，但未重新运行计时或 SfM，详见 [复查记录](QUALITY_RECHECK.md)。

### 8.1 十张 Kitchen 图片的 SfM 流程

数据来自本机 `data/datasets/nerfstudio/kitchen/`，从 `transforms.json` 按顺序取前 10 张连续视图。它们是原始重叠视图；脚本只在一份临时副本上额外生成 5 张已知变体（完全复制、JPEG 重编码、模糊、变暗、变亮）。筛选清单保留全部 10 张原图，只排除这 5 张人为加入的变体，不根据检测告警删除普通视图。每张源图的 SHA-256 记录在报告中。数据集镜像版本与归档哈希见 [数据集测试计划](DATASET_TEST_PLAN.md) 及本机未入库的 Nerfstudio 下载清单。

在仓库根目录使用项目虚拟环境运行；输出目录必须是新的、尚不存在的目录：

```bash
source .venv/bin/activate
# WSL/Linux 中需已安装 COLMAP，且 colmap 可从 PATH 调用。
python -m scripts.quality_sfm_smoke \
  --source data/datasets/nerfstudio/kitchen \
  --output outputs/quality-sfm-smoke-new \
  --count 10 \
  --camera-model OPENCV \
  --max-image-size 640 \
  --max-features 4096 \
  --threads 2 \
  --matcher sequential \
  --overlap 5
```

SfM 在 CPU 上运行，使用独立相机；没有从 `transforms.json` 导入位姿或相机内参。当前一次结果如下：

| 输入 | COLMAP 注册数 | 原始 10 张注册数 | 稀疏点 | 用时 |
| --- | ---: | ---: | ---: | ---: |
| 10 张原始视图 | 10/10 | 10/10 | 1,117 | 26.631 秒 |
| 10 张原图 + 5 张已知变体 | 14/15 | 10/10 | 1,182 | 32.104 秒 |
| 人工确认仅保留原图 | 10/10 | 10/10 | 506 | 18.098 秒 |

单次运行的注册数、稀疏点数与用时会受输入及运行环境影响。变体加入或移除后稀疏点数变化不等于几何精度变化；该 smoke 也没有对点云做真值对齐或误差评估。

该结果记录于 2026-11-03，三组均返回 `rc=0`，主报告记录 `success=true`，保存在 `outputs/quality-sfm-smoke-20261103-current/`。此处按已知变体身份手工指定清单，仅证明流程与实际输入一致；检测命中后复核排除的正式副本对照见 8.7。

### 8.2 500 张图片检测速度 smoke

输入来自同一 Kitchen 数据集的 617 张图：按排序后的文件名均匀抽取 500 个索引并包含两端，不复制图片凑数。输入清单记录了源归档版本、哈希、抽样索引和各文件 SHA-256。该次直接调用 `image_quality.analyze`，没有保存网页筛选结果，也没有运行 SfM。

使用同一归档的数据可按下面方式重选样本并重跑检测计时：

以下计时示例使用 Python `resource` 模块，仅适用于 WSL/Linux；Windows 原生 Python 请在 WSL/Linux 中运行。

```bash
source .venv/bin/activate
python - <<'PY'
import json
from pathlib import Path
import resource
from time import perf_counter
from recon_workbench.image_quality import analyze

root = Path("data/datasets/nerfstudio/kitchen").resolve()
image_root = root / "images"
paths = sorted(image_root.glob("*.jpg"))
if len(paths) != 617:
    raise SystemExit(f"源图片数应为 617，实际为 {len(paths)}")
indices = [round(index * (len(paths) - 1) / 499) for index in range(500)]
images = [{"name": paths[index].name, "path": paths[index].relative_to(root).as_posix()}
          for index in indices]
started = perf_counter()
report = analyze(root, images)
print(f"elapsed_seconds={perf_counter() - started:.2f}")
print(f"peak_rss_kib={resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}")
print(json.dumps(report["counts"], ensure_ascii=False, indent=2))
PY
```

2026-11-03 单次结果：500/500 张可解码且文件哈希唯一，输入总量 57,859,246 字节，约 55.2 MiB。全部 124,750 个唯一图片对均计算哈希距离，其中 58,601 对符合共同条件、参与最终判定。dHash 和 pHash 各命中 9 对，联合命中 4 对；报告展示 200 对，展示已截断。一次调用耗时 **81.49 秒**，进程峰值 RSS 为 **127,984 KiB**。环境为 WSL2、Python 3.13.12、Pillow 13.0.0、ImageHash 4.3.2、NumPy 2.5.4、SciPy 1.18.1。默认策略参数和质量模块 SHA-256 记录在 `outputs/quality-speed-20261103-current/` 的 `quality.json` 与 `speed.json`。

这是一场景、一次运行的计时，不是通用吞吐基准；测量包含导入依赖、读取、解码、特征计算、全量两两比较与报告组装，不包含 JSON 写盘。没有清缓存，也没有控制其他系统服务。由于输入清单和结果位于本机忽略目录，换机器时需先按 [数据集测试计划](DATASET_TEST_PLAN.md) 准备同版本数据，再重新生成清单；单次计时不能用于跨机器比较。

81.49 秒绑定于修复前的算法文件，其原始字节保存在 `outputs/quality-fixes-20261105/frozen-source/`。边界修复后的特征回放确认这 500 张的全部 124,750 对判定、展示及逐图证据不变；本轮没有重新计时，不能将该耗时作为新版本速度实测。

### 8.3 配对评估脚本

合成 smoke 用于确认评估管线和阈值行为：

```bash
python -m scripts.evaluate_quality \
  --synthetic-smoke \
  --output outputs/quality-evaluation-synthetic
```

真实照片应先用 `scripts.make_quality_benchmark` 生成源保留的变体清单，再用
`--negative-pairs` 加入人工复核的“不同视角”图片对；生成的压缩、缩放等变体不能直接当作真实相机视角标签。清单中的 `sha256` 可省略，评估报告会记录实际哈希；若填写了错误哈希，评估会拒绝运行。报告分别给出 dHash、pHash 和联合判定的 precision/recall/FPR/FNR，并把未复核的 challenge 对单独列出。真实视角误报/漏报需使用明确来源的人工标签、固定阈值后的 validation split，并披露复核者是否已知算法输出；当前样本的具体限制见 8.5。

例如：

```bash
python -m scripts.make_quality_benchmark \
  --source kitchen=data/datasets/nerfstudio/kitchen/images \
  --output outputs/quality-benchmark-kitchen \
  --limit 20 \
  --negative-pairs data/quality/kitchen-negative-pairs.json
python -m scripts.evaluate_quality \
  --manifest outputs/quality-benchmark-kitchen/pairs.json \
  --output outputs/quality-evaluation-kitchen
```

### 8.4 边界与产物

- 10 张流程检查和 500 张单次计时分别验证流程与检测规模，不能代替筛选前后对照、准确率标注、跨场景测试或几何质量评估。
- 截至 2026-11-04，已有 Kitchen 与 Dozer 的真实照片对标注评估，细节见 8.5。Kitchen 验证集为 50 对；Dozer 修订后为 49 对不同视图、1 对待定，没有重复正例，不能评估其重复召回率。更早的合成 smoke 和生成变体只用于验证评估管线，不代表真实视角准确率。这些有限、分层选择的样本仍不能外推为通用准确率或证明跨场景重复检测能力。
- 报告、临时输入、SfM 输出和日志保留在本机 `outputs/quality-sfm-smoke-20261103-current/` 和 `outputs/quality-speed-20261103-current/`，不提交到 Git。SfM 主报告为 `quality-sfm-smoke.json`；速度结果为 `speed.json`，抽样与输入清单为 `input.json`。换机器复测时记录新报告、环境、参数和输入哈希，不应覆盖或改写已有记录。

### 8.5 真实视角复核与阈值对照

Kitchen 使用 100 对真实照片，校准集与验证集各 50 对，163 张唯一图片没有跨 split 重用。候选分层覆盖算法命中、哈希分歧、阈值附近、文件序列相邻及较远视图。最终校准标签为 23 对近似重复、27 对不同视角；验证标签为 13 对近似重复、37 对不同视角。“不同视角”表示两张图片存在有意义的视角差异，不证明每张照片对整个重建集合都不可替代。

只用校准集以联合判定 F1 选择阈值，随后固定阈值评估验证集。候选阈值为 `dHash ≤ 8`、`pHash ≤ 16`；校准集联合 TP=14、FP=1、FN=9、TN=26，precision=0.933、recall=0.609。两个阈值均触及搜索范围上限，不能宣称已找到稳定最优值。应用默认阈值仍为 4/6。

以下为 2026-11-03 最终验证标签上的结果。各方法共用当前特征与判定条件；默认 4/6 行按保存的逐对距离重新计数，dHash 4 对应既有 dHash 的默认阈值基线：

| 方法 | 验证 precision | 验证 recall | FP | FN | TN | TP |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| dHash，默认阈值 4 | 0.750 | 0.231 | 1 | 10 | 36 | 3 |
| pHash，默认阈值 6 | 1.000 | 0.308 | 0 | 9 | 37 | 4 |
| 联合判定，默认阈值 4 / 6 | 1.000 | 0.154 | 0 | 11 | 37 | 2 |
| dHash，阈值 8 | 0.786 | 0.846 | 3 | 2 | 34 | 11 |
| pHash，阈值 16 | 1.000 | 0.846 | 0 | 2 | 37 | 11 |
| 联合判定，候选阈值 8 / 16 | 1.000 | 0.769 | 0 | 3 | 37 | 10 |

默认联合判定相比 dHash 4 将误报从 1 对减少到 0 对，同时将漏报从 10 对增加到 11 对；默认召回仍低。候选 8/16 相比 dHash 8 也减少了本批误报，但召回较低。两档联合判定均未优于本批对应的 pHash 单算法。零误报只表示这 37 个分层选择的负例中未见误报，不能外推总体 FPR。联合判定的收益与代价需一起报告。

Dozer 使用从源场景 359 张中均匀抽取的本机 50 张子集，分层生成 50 对。阈值附近/哈希分歧候选为 6 对，其余包含相邻和较远视图。2026-11-04 最终标签为 **49 对不同视角、0 对近似重复、1 对待定**。待定项不计入混淆矩阵，使用固定阈值的结果如下：

| 方法 | TP | FP | FN | TN | precision | recall |
|---|---:|---:|---:|---:|---|---|
| 联合判定，默认阈值 4 / 6 | 0 | 0 | 0 | 49 | 不可计算 | 不可计算 |
| 联合判定，候选阈值 8 / 16 | 0 | 0 | 0 | 49 | 不可计算 | 不可计算 |
| pHash，阈值 16 | 0 | 5 | 0 | 44 | 0.000 | 不可计算 |

Dozer 没有重复正例，因此本批只能补充负例误报检查，不能说明重复召回率为 100% 或跨场景重复检测有效；没有用 Dozer 调整阈值。

**标签来源与结论限制：** 两批最终标签包含在算法结果已知后的复核，助手作出的目视判断经用户授权采用，不构成独立盲审验证。校准集未随验证标签复核而改变，8/16 仍由原校准集选择。候选按检测行为等因素分层，不能用标签比例估计整个场景的重复率。扩大结论仍需更有代表性的样本及其他场景的独立标注。

当前使用的证据保存在本机 Git 忽略目录：

| 项目 | 最终标签与报告 |
|---|---|
| Kitchen 标签 | `data/quality/kitchen-view-review/human-labeled/pair_labels_reviewed_revised_20261103.csv` |
| Kitchen 评估 | `outputs/quality-kitchen-view-eval-20261103/validation-report-revised-20261103.json`；校准过程见同目录 `threshold-calibration.json` 的校准部分 |
| Dozer 标签与选对清单 | `data/quality/dozer-review-20261103-external-validation-final/pair_labels_reviewed_revised_20261104.csv`，同目录 `review-candidates.json` |
| Dozer 评估 | `outputs/quality-dozer-validation-revised-20261104/scene-quality-evaluation.json` |

原始标签、旧报告、修订依据及哈希记录保留供追溯；上表结果统一使用最终标签。

### 8.6 模糊/曝光响应和质量告警筛选对照

本节记录模糊/曝光检测依据、受控变换响应，以及质量告警筛选前后的稀疏 SfM 对照。
完整参数、分组、三次重复的中位数与范围、限制和代码入口见
[图片质量与筛选重建验证](QUALITY_RESULTS_20261103.md)。

连续 Kitchen 样本四组各运行三次，均成功；各组原图注册数中位数为 52/60。
一组筛选运行有一次注册到 53/60，但未形成重复一致的覆盖增益。
误差、位姿参考一致性和耗时的变化方向不一致，结果不支持筛选稳定提升重建质量或速度。
受控退化样本的告警响应不等同于真实照片分类准确率；自然相邻视图筛选、更多场景和人工质量标签仍需独立验证。受控同图副本的筛选对照见 8.7。
本次未更改应用默认阈值或产品自动选择行为。

### 8.7 重复副本筛选与重建对照（2026-11-04）

固定 Kitchen 前 60 张连续原图，再增加 15 张副本：5 张完全复制、5 张 JPEG
重编码、5 张 75% 缩放。默认 SHA-256 与联合 dHash≤4 / pHash≤6 检测命中全部
15 张；经用户授权的助手逐对复核后排除，得到 **75→60 张，减少 20%**。
没有删除自然拍摄视图，原始 60 张及其中 2 张模糊告警图片全部保留。

三组按固定顺序轮换，各自独立重建 3 次，9/9 均成功且每次只有 1 个模型。
参数为 COLMAP 3.7 CPU、OPENCV 独立相机、最大边 1000、名义 SIFT 上限 4096、exhaustive、
4 线程，不提供发布内参或位姿。下表三个注册数对应三次重复：

| 组 | 输入 | 原图注册数（分母 60） | SfM 耗时秒中位数 [最小, 最大] |
|---|---:|---|---|
| original | 60 | 52 / 52 / 52 | 426.543 [422.049, 430.699] |
| augmented | 75 | 52 / 53 / 52 | 659.640 [655.754, 691.022] |
| filtered | 60 | 52 / 52 / 52 | 424.331 [421.894, 435.349] |

本批 SfM 耗时中位数减少 **35.7%**，不包含检测和复核时间。第 1、3 轮筛选前后
原图注册名单相同；第 2 轮筛选后少注册 `frame_00018.jpg`，但该图仍在输入中。
不能将这一次差异单独归因为删除副本，也不能宣称筛选严格不影响覆盖。
original 与 filtered 输入逐字节一致，两组间的小幅统计差异不解释为筛选改善。

预先固定的第 17 帧对齐锚点在全部运行中均未注册，发布位姿一致性不可计算，
没有事后更换锚点。本批只支持同图副本清理的计算收益，不证明自然相邻视图
可以安全自动删除，也不提供几何精度提升结论。

协议见 [重复副本筛选与 SfM 对照](DUPLICATE_SFM_PROTOCOL.md)，完整结果、
内部统计、原图增减与复现入口见 [重复副本筛选与重建对照](DUPLICATE_SFM_RESULTS_20261104.md)。
输入、复核、逐次模型、日志和最终核验保存在本机
`outputs/duplicate-sfm-20261104/`，不提交数据。新增脚本复用既有 SfM 执行流程；
2026-11-04 当批 Python 回归为 149/149 通过，产品默认阈值和自动选择行为没有修改。
