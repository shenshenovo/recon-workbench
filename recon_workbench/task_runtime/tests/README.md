# 专属测试

已实现队列、资源、阶段恢复和备份恢复测试；测试清单见 [testing.md](../docs/testing.md)。

## 组织

- PR1：`test_runtime.py`，HTTP 回归在根目录 `tests/test_workspace.py`。
- PR2：新增 `test_resources.py`，扩展 API、取消及迁移测试。
- PR3：新增 `test_recovery.py`、`test_backup.py`，扩展 API、迁移和历史记录回归。
- 受控子进程在临时工作区生成，不读写真实照片和数据库。
- 同步使用事件、管道或就绪文件加明确超时，不靠固定 sleep 假设执行顺序。

## 命令

```bash
# 专属测试
python -m unittest discover -s recon_workbench/task_runtime/tests -t . -v

# 全仓库回归；会同时发现根目录 tests/ 和本包测试
python -m unittest -v
```

每个 PR 至少执行专属测试和全仓库回归；真实子进程清理、真实算法、真实 GPU 与浏览器手动验收分别记录。
没有 GPU 时可运行假设备调度测试，但必须将硬件验收标为“未执行”，不能以 mock 测试代替。
