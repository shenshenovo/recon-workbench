# SfM 补图扩展与版本

工作台选择“扩展已有 SfM”，选择当前项目成功的父任务，再选择新照片，检测质量并保存筛选。每次扩展创建独立任务；项目记录显示父版本与补图数量。

```bash
python main.py --method sfm --images data/new_photos --base-sfm outputs/base_task --output outputs/extended_task
```

父任务必须包含 `input_manifest.json`、不可变输入、数据库和所选二进制模型。历史任务没有指纹时不能安全补建，需重新生成基线。支持继续扩展已成功的增量版本。

全量 SfM 保存输入副本与 SHA-256 清单。增量校验旧输入，拒绝重复照片，复制数据库及模型；SQLite backup 捕获一致的数据库快照。新照片使用内容摘要加序号命名，仅提取补图特征，exhaustive 匹配复用已有匹配，再使用 `mapper --input_path` 续建。旧相机位姿允许优化。父版本不改写；失败保留新任务日志。COLMAP 续建直接输出的模型目录与普通编号子目录均支持。

摘要新增 `images_path`；增量摘要增加 `base_sfm`、`added_images`、`added_registered_images`。新增照片全部未注册时失败，部分注册时警告。新版 MVS 重新生成，新版 3DGS 重新准备数据训练；不支持续训。

HTTP 启动请求增加 `parent_task_id`，仅接受当前项目已完成的 SfM 任务。SQLite tasks 增加可空同名字段，启动时迁移，历史任务为空。网页禁止提交原始 `base_sfm` 路径。CLI 接受本机父目录。

## 真实验证（2026-11-02）

WSL Ubuntu 22.04，Python 3.10，COLMAP 3.7 CPU，4 线程、1200 最大边长、SIMPLE_RADIAL、exhaustive。现有上传照片共 8 张，全量注册 8/8、6293 点、平均重投影误差 0.5652 px，路线耗时约 28.5 秒。结果在本机 `outputs/uploaded-sfm-validation`。这些指标不代表真实几何精度。

真实 6→8 增量烟雾测试通过：补图 2/2 注册，最终 8/8、6264 点、平均重投影误差 0.5651 px、路线耗时约 18.7 秒。父版本保留于 `outputs/incremental-base6`，新版位于 `outputs/incremental-extended8-v3`。

正式 50→70 计算与文件验收已完成，三组全部注册，详见 [完整实测记录](SOUTH_BUILDING_ACCEPTANCE.md)。2026-11-05 历史点云、MVS 点云和网页 6→8 增量启动闭环实际验收通过，截图与测试索引见 [交付材料](ZHOU_DELIVERY.md)。

准备至少 70 张同场景照片后运行：

```bash
python -m scripts.run_incremental_benchmark --images data/scene/images --output outputs/comparison_new
```

脚本固定前 50 与后 20，保存指纹，依次运行 50 张基线、70 张增量、70 张全量，记录日志和 comparison.json。基线注册率不足 90% 时停止。视觉验收和截图需另做；几何差异须先做相似变换对齐。
