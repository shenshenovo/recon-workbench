# 新版 SfM 下游验收（2026-11-05）

输入：`outputs/south-building-50-70/incremental70/sfm`，70 张已注册照片、56977 个稀疏点。SfM 原结果保持独立保存。

## 3DGS 重新训练

数据准备使用现有项目适配器生成 `outputs/acceptance-new-sfm-3dgs/3dgs/dataset`，含 70 张照片、相机位姿和稀疏模型。

Linux Brush v0.3.0 在当前 WSL 下未能训练：未安装 Vulkan 时找不到适配器；安装 Mesa Vulkan 后仅能发现 llvmpipe CPU 设备，没有 NVIDIA Vulkan 设备。`nvidia-smi` 可用不代表 Vulkan 可用。

改用官方 Windows Brush v0.3.0，从同一新版数据集重新训练：

```powershell
& .\.tools\brush-windows\brush_app.exe .\outputs\acceptance-new-sfm-3dgs\3dgs\dataset --total-steps 500 --max-resolution 800 --max-splats 1000000 --export-every 500 --export-path .\outputs\acceptance-windows-brush --export-name 'export_{iter}.ply' --seed 42
```

进程退出码 0，生成 `export_500.ply`，14086266 字节；项目 Gaussian PLY 解析器验证为 59681 个高斯。新版数据准备、重新训练和导出衔接通过。500 步仅作为流程验收，未进行最终渲染质量或留出视图评估，也未验证继续旧训练状态。

## MVS

官方 OpenMVS v2.4.0 Ubuntu x64 ZIP 完整性校验通过，但二进制依赖 GLIBC_2.38 和 GLIBCXX_3.4.31/32，不能直接在现有 Ubuntu 22.04 启动。

兼容方案：保留系统核心库，使用官方 v2.4.0 源码在当前发行版构建；最终构建与真实验收结果见下节。

官方 OpenMVS v2.4.0 源码位于 WSL `/home/jiturine/openmvs-acceptance-src`。VCG 的 Git 与 codeload 下载曾超时，最终使用用户下载的完整官方 ZIP；依赖与源码已齐备，没有修改系统 GLIBC。

## MVS 验收补充：已通过

用户提供 VCG ZIP 后完整性校验通过，配置和构建成功。为适配 Ubuntu 22.04，外部 OpenMVS 源码做以下本地修改：JPEG XL 检测改为可选并禁用不受当前 OpenCV 支持的 JXL 保存；nanoflann SearchParameters 适配旧版 SearchParams（保留排序参数语义）；CGAL AABB 头文件使用旧版对应名称。未修改系统 GLIBC。补丁保存于 `.tools/openmvs/ubuntu22-compatibility.patch`。

编译目标为 InterfaceCOLMAP、DensifyPointCloud，关闭 CUDA 和查看器。该本地安装只验收 dense 阶段，网格和纹理工具仍不可视为可用。

真实命令：

```bash
python main.py --method mvs --sfm-output outputs/south-building-50-70/incremental70/sfm --output outputs/acceptance-new-sfm-mvs --mvs-stage dense --openmvs-bin /home/jiturine/openmvs-acceptance-build/bin --max-image-size 800 --dense-resolution-level 1 --threads 4
```

结果：70 张注册输入，351916 个稠密点，整条路线 139.007 秒，任务 completed。项目 cloud_packet 解析通过，网页预览均匀抽样到 175958 点。输出为 `outputs/acceptance-new-sfm-mvs/mvs/dense/dense.ply`。新版 SfM 到重新生成 MVS 稠密点云的流程验收通过；不代表网格、纹理或真实几何精度验收通过。
