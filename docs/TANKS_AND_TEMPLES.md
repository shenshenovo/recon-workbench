# 300～500 张真实重建数据集：Tanks and Temples

2026-10-29 已在当前 GPU 工作目录下载三个完整场景，合计 **1082 张原始 RGB 图片**。随后 Train 完成 SfM → MVS，Barn、Meetingroom 尚未重建。场景用途是本项目的测试定位，不代表数据作者的应用声明。

| 场景 | 原始照片数 | 分辨率 | RGB 文件合计 | 可观察的问题与应用意义 |
|---|---:|---|---:|---|
| Train | 301 | 1920×1080 | 192.4 MiB | 工业遗产数字化与大型物体重建：观察车体轮廓、轮组、细杆与环绕覆盖 |
| Barn | 410 | 1920×1080 | 504.8 MiB | 建筑记录与外立面重建：观察墙面、屋顶、木板纹理及大范围闭合 |
| Meetingroom | 371 | 1920×1080 | 397.6 MiB | 室内空间记录：观察桌椅遮挡、重复结构、弱纹理墙面与空间覆盖 |

以上均为官方预采样照片包的完整数量，不是重复或抽帧凑数。只选用每个场景 `images/` 中的一套 RGB；没有导入发布相机位姿、已有重建或几何真值。

## 直接用于工作台的目录

```text
data/datasets/tanks-and-temples/Train/images
data/datasets/tanks-and-temples/Barn/images
data/datasets/tanks-and-temples/Meetingroom/images
```

工作台选择“原始照片”，输入其中一个 `images` 路径，检测并保存选择后再启动重建。三个场景都在当前网页 500 张限制内，单场景 RGB 总大小也低于 1 GiB。

建议先测试 **Train 301 张**，再试 **Barn 410 张**，最后用 **Meetingroom 371 张**观察弱纹理、重复椅子、遮挡与曝光差异的影响。对当前系统的容量结论仍需实际运行；下载完整不代表图像质量已筛选或重建已通过。

首轮 SfM 可使用 SIMPLE_RADIAL、共享相机、最大边长 1200、CPU 4 线程。原始照片文件保持 1920×1080，最大边长只作用于重建处理。官方没有提供精确相机内参，官网另给出针孔模型的经验焦距初值 `fx=fy=0.7W`、主点 `W/2,H/2`，它不是标定真值；若初始化或注册困难，应进一步核对内参与镜头模型。

本机试跑命令（这是示例，未在本次下载任务中执行；输出目录必须不存在）：

```bash
cd /home/shenshenovo/recon-workbench
/home/shenshenovo/miniconda3/envs/recon-fast3r/bin/python main.py --method sfm \
  --images data/datasets/tanks-and-temples/Train/images \
  --output outputs/t2-train301-sfm-first \
  --colmap /home/shenshenovo/recon-workbench/.tools/colmap-deb/colmap \
  --camera-model SIMPLE_RADIAL --single-camera --max-image-size 1200 --threads 4
```

## 校验与来源

- 三个 ZIP 来自官网所用的 `storage.googleapis.com/t2-downloads/image_sets/` 存储，下载地址固定到 GCS generation，并保留 ETag、服务端 MD5 和本地 SHA-256。
- 三包 MD5 均与服务端元数据匹配；ZIP CRC 全部通过。
- 1082 张照片均完整解码，每个场景的 SHA-256 均无重复。
- 每场景 `manifest.json` 保存原始包内路径、逐文件 SHA-256、尺寸、大小与应用定位。
- `inventory.json` 汇总本机数量、验证结果与路径；`_downloads/download-manifest.json` 保存下载证据，原始 ZIP 保留在 `_downloads/`。
- `_original/` 保留解压结构，`images/` 为对应 RGB 的硬链接，没有重新编码；原 ZIP 可用于重新验证和恢复。

## 官方基准与几何真值

- [Tanks and Temples 官方下载页](https://www.tanksandtemples.org/download/)
- [数据集论文](https://storage.googleapis.com/t2-downloads/paper/tanks-and-temples.pdf)
- [官方许可原文](https://www.tanksandtemples.org/license/)；本机保存于 `_downloads/source-license.html`。教学、报告中应注明来源和论文。

Barn 与 Meetingroom 属于 training 场景，官网提供公开几何真值入口；链接已保存到各自 `manifest.json`，**本次未下载真值**。Train 属于 intermediate 评测组，本次没有取得公开几何真值。后续若要计算几何精度，需要另取适用真值并执行对齐与评估，不能把普通重建点数当作精度。

## 场景预览

- [Train 原图概览](../data/datasets/tanks-and-temples/Train/overview.jpg)
- [Barn 原图概览](../data/datasets/tanks-and-temples/Barn/overview.jpg)
- [Meetingroom 原图概览](../data/datasets/tanks-and-temples/Meetingroom/overview.jpg)

图片仅为文件抽样缩略图，实际重建输入使用完整原图。数据和下载包位于忽略的 `data/`，不会随 Git 源码提交；另一台电脑需要另外取得这些文件。

## Train 301 张首次实测

已完成全部 301 张的 SfM → MVS。顺序匹配、SIMPLE_RADIAL 共享相机、最大边长 1200、CPU 4 线程：301/301 张注册到同一个模型，162,324 个稀疏点、6,692,989 个稠密点。SfM 约 21 分 37 秒，MVS 额外约 14 分 31 秒，路线约 36 分 8 秒。

公开运行摘要见 [GPU 实测记录](GPU_EXPERIMENTS.md)。GPU 电脑的完整报告与预览保存在 `outputs/t2-train301-sequential-first/README.md`，该目录未上传 GitHub。这次使用顺序匹配，与 kitchen 200 张的全量匹配条件不同，不用于仅比较图片数量的耗时关系；也不构成硬件容量上限。
