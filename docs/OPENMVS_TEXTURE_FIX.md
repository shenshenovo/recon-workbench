# OpenMVS 2.3 OBJ 导出崩溃的本机修复

## 原因与补丁

2026-11-25，100 张照片的任务已完成 SfM、稠密点云和网格；TextureMesh 在日志打印 `Mesh texturing completed` 后以 -11 退出，未生成 OBJ/MTL。

本机 OpenMVS 2.3 的 `Mesh::SaveOBJ` 直接读取 `faceTexindices[idxFace]`。单张纹理图时，这个数组可以为空；[上游问题 #1132](https://github.com/cdcseacave/openMVS/issues/1132)报告了同样的越界崩溃。补丁将空数组解释为使用第 0 张纹理：

```cpp
const auto texIdx = faceTexindices.empty() ? 0 : faceTexindices[idxFace];
```

## 在已有静态编译目录上生成修正版

从集成分支根目录执行，`--source` 指向本机已有源码和 `make/` 编译目录：

```bash
python -m scripts.build_openmvs_objfix --source /home/shenshenovo/Compiler/openMVS
mkdir -p .tools/openmvs/bin
ln -s ../../openmvs-objfix/bin/TextureMesh .tools/openmvs/bin/TextureMesh
```

脚本只重新编译 Mesh.cpp，复制既有静态库并重新链接 TextureMesh；输出位于忽略的 `.tools/openmvs-objfix/`。原源码、静态库和程序保持不变，并另存 `TextureMesh.original` 备份。需要原机器的 C++ 编译器、头文件及链接库；它不是可在任意环境运行的预编译安装器。

脚本严格匹配已审查的旧导出语句，源码不匹配时停止。输出目录必须不存在，以免覆盖已有修复记录。`build-manifest.json` 保存补丁前后的源码与二进制 SHA-256、编译和链接命令，`build.log` 保存构建日志。修复后的版本字符串仍为 2.3.0，通过哈希区别原版。

工作台优先从 `.tools/openmvs/` 发现 TextureMesh，其他 OpenMVS 工具继续从原路径发现。回退时移除本机覆盖链接即可使用原程序。

## 恢复本次任务

原失败任务保留，不将其改写成成功。校验已有 mesh 阶段清单后，新建 `scene-texture-fixed` 任务，记录原任务 ID；复用原 SfM、稠密点云和网格，只执行纹理导出。

更换二进制会改变执行指纹，原任务的通用恢复预检因此可能要求保守重做。不能篡改旧指纹：本次采用独立新任务与显式的 MVS 产物清单校验，记录新工具指纹，再从 `textured` 边界运行。

当前本机修复任务 ID：`39496e7a15da4f39a3a0d42d77c8f403`。原任务 ID：`d79e3aa2b864423da923d6ad384598a6`。完整日志和构建产物留在本机，不随源码发布。

## 实际验证结果

修复任务已完成并登记 7 个模型产物。只重算纹理的本次路线用时 114.3 秒，不含前置 SfM/稠密/网格耗时与后续发布排查时间。最终 OBJ 有 238,010 个顶点、475,583 个三角面、1,426,749 个纹理坐标；全部索引范围和坐标有限性检查通过。MTL 及其引用的 4096×4096 JPEG 存在，纹理图完整解码通过。原失败任务继续保留 failed 状态。

本次恢复还发现：MVS 复用 SfM 时，旧摘要中的稀疏点云路径可能在新任务目录外，导致严格的产物登记事务回滚。现在由 COLMAP 从已验证的相机/三维点模型导出当前任务自己的 `sparse.ply`，无需依赖或读取旧摘要所指的 PLY。新增回归覆盖“来源 PLY 缺失，仍能登记新任务的稀疏与稠密点云”。

修复任务的稠密点数仍为 1,629,689，稀疏点数为 48,559，注册数为 100/100。本机验证详情见 `outputs/integration-validation/texture-repair-result.json`。
