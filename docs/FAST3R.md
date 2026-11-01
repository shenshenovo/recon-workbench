# Fast3R：RTX 4060 8GB 首次运行

集成状态：统一 CLI、网页参数、任务日志、点图过滤与 PLY 展示已接入；已在 RTX 4060 Laptop 8GB 的 WSL 环境完成真实模型推理。512 尺寸的 32～64 张和 224 尺寸的 200 张均有成功记录，但容量成功不等于点云质量通过。完整结果与限制见 [GPU 实测记录](GPU_EXPERIMENTS.md)。自动化中的模拟产物不作为模型运行证据。

## 1. 部署位置和首次规模

在有 RTX 4060 8GB 的电脑上运行整个工作台，浏览器访问该电脑的 `http://127.0.0.1:8765`。本次没有实现 Mac 自动调用另一台电脑 GPU 的远程调度。

以下按 Linux 或 WSL 的 Bash 终端编写，可先执行 `uname -a` 确认。原生 Windows 不适合直接运行当前使用 `fcntl` 的网页后端，建议在 WSL 中操作。

第一次选 2～4 张相邻且重叠的照片，512 预处理、BF16、数量保护上限 8。验证后再尝试 8 张。官方在 A100 上的 320 张测试峰值显存约 41.9 GiB，不能据此承诺 8GB 显卡可处理 200～300 张。[官方结果](https://fast3r-3d.github.io/)

## 2. 独立安装环境

先将本项目代码复制或解压到 GPU 电脑，在仓库根目录执行。不要在 Mac 的 `.venv` 安装以下 CUDA 依赖。

```bash
conda create -n recon-fast3r python=3.11 -y
conda activate recon-fast3r
python -m pip install torch==2.5.1 torchvision==0.20.1 --index-url https://download.pytorch.org/whl/cu124
python -m pip install -r requirements-fast3r.txt
```

这是 [PyTorch 官方提供的 CUDA 12.4 安装组合](https://pytorch.org/get-started/previous-versions/)。`nvidia-smi` 中的 CUDA 13.0 是驱动支持版本，不等于当前 Python 中的 PyTorch CUDA 运行时；不需要为了安装此组合卸载现有驱动。

安装独立的 Fast3R 官方源码，固定本次核对的版本：

```bash
git clone https://github.com/facebookresearch/fast3r.git .tools/fast3r
git -C .tools/fast3r checkout 33104d4b5b8df43795ecded236194958bbdac572
python -m pip install -e .tools/fast3r --no-deps
python -m recon_workbench.fast3r --check-env
```

`.tools/fast3r` 已存在时跳过 clone，核对目录和版本即可。最后一条应输出 GPU、显存和 PyTorch/CUDA 版本，并成功导入 Fast3R；它不会下载权重。

`requirements-fast3r.txt` 安装本适配器所需的推理依赖，不安装上游训练用的 DeepSpeed 或可视化服务。注意力配置使用官方支持的 `pytorch_auto`，不安装 cuROPE，不启动 Fast3R 自带的 Gradio/Viser 服务。参考 [官方安装与 FAQ](https://github.com/facebookresearch/fast3r)。

## 3. 数据与小样例

本机准备了 4 张 ETH3D courtyard 相邻照片，源图是已有去畸变 RGB，没有导入发布相机位姿或三维点：

```text
data/benchmarks/eth3d/courtyard_fast3r_004/images/
```

`subset.json` 记录文件名、来源和 SHA-256。数据不在 GitHub 中，需要把此目录复制到 GPU 电脑；也可以在网页上传自己的 2～4 张重叠照片。只有代码没有照片时，CLI 会明确报告目录不存在或照片数量不符。

首轮命令（从仓库根目录执行）：

```bash
conda activate recon-fast3r
python main.py --method fast3r \
  --images data/benchmarks/eth3d/courtyard_fast3r_004/images \
  --output outputs/fast3r-courtyard-first \
  --fast3r-size 512 \
  --fast3r-max-views 8 \
  --fast3r-precision bf16
```

默认权重为 `jedyang97/Fast3R_ViT_Large_512`，首次运行需要从 Hugging Face 下载，加载与推理分别计时。如果网络不通，先取得模型完整目录（包括 `config.json` 与权重），再通过 `--fast3r-model /模型目录的绝对路径` 指定。也可预先下载：

```bash
python -c "from huggingface_hub import snapshot_download; snapshot_download('jedyang97/Fast3R_ViT_Large_512', local_dir='.tools/fast3r-model')"
```

首次成功后换新输出目录重跑以观察缓存后的耗时；输出目录已存在时不会覆盖。

## 4. 网页操作

必须从 GPU 环境启动服务：

```bash
conda activate recon-fast3r
python app.py --no-browser
```

在 GPU 电脑的浏览器打开工作台，上传小样例或选择原始照片目录，检测质量、确认并保存 2～4 张照片，下一步选 **Fast3R · 学习式点云（CUDA）**。

- 512 是首轮使用的常用尺寸；224 是中心裁剪的小尺寸试跑选项，结果可能损失覆盖，需单独验证。
- 数量上限默认 8，超过会拒绝启动，调高该数字不等于显卡有更多显存。
- BF16 是首轮设置；上游注意力内部也有精度转换，不能把 FP32 选项理解为所有运算严格使用 FP32。
- 默认逐视图过滤低于 50% 置信度分位数的点，再按视图分配预算，最多导出 30 万点。过滤分位数不是准确率，导出数量也不是表面质量。
- 此路线不调用 COLMAP，不使用页面中的 SfM 相机内参，不对当前场景进行训练。

完成后在结果区旋转、缩放点云；项目记录与实验对比页可以找到任务。CLI 任务在下一次服务启动时会作为历史结果导入，网页启动的任务则立即进入当前项目。

## 5. 输出与故障定位

```text
outputs/任务目录/
  config.json                 # 统一入口请求
  status.json / summary.json  # 当前路线状态和结果
  fast3r/
    config.json / input.json  # 推理参数及实际文件哈希
    logs/01_inference.log    # 模型加载、前向推理与导出日志
    pointcloud.ply           # 普通 XYZ/RGB 点云，WebGL/MeshLab 可查看
    pointcloud_stats.json    # 各视图阈值、有效点和导出数量
    summary.json             # 输入数、尺寸、精度、版本、计时与显存记录
```

验收条件：实际处理的文件名单与选择一致；成功导出非空且可读的点云；网页能显示；日志与计时可追溯。输出目录中的 `summary.json` 才是本机模型运行结果。

如失败，先提供 `--check-env` 输出及 `fast3r/logs/01_inference.log` 的末尾。显存不足时先减少为 2 张，再尝试 224 尺寸；适配器不会自动分组处理后直接拼接局部坐标系。权重下载失败和显存不足分别处理。

`peak_gpu_allocated_bytes` 是 PyTorch 记录的峰值分配显存，不是 `nvidia-smi` 的整机显存峰值。路线总耗时还包含子进程、权重加载、预处理和导出，不能只拿前向推理时间与 SfM 全流程比较。

## 6. 当前边界

这里只汇集公共坐标系中的全局点图，过滤并采样后导出，未执行表面融合。当前未导出相机位姿、COLMAP 模型或网格，也不直接将 Fast3R 结果送入 MVS/Brush。处理照片数不当作 SfM 注册数，不生成注册率、重投影误差或新视角 PSNR/SSIM。

200、300 张 kitchen 子集可用于现有路线的数量测试，具体目录见 [数据集计划](DATASET_TEST_PLAN.md#9-kitchen-数量阶梯与-fast3r-接入准备)。Fast3R 容量实验另用嵌套视图名单；224/200 的成功不能解释为 512/200 已通过，也不能解释为房间几何质量合格。当前默认照片保护上限仍为 8，容量试验需显式调整。

第三方代码和权重遵循 [Fast3R 发布者许可证](https://github.com/facebookresearch/fast3r/blob/main/LICENSE)，未随项目源码复制发布。
