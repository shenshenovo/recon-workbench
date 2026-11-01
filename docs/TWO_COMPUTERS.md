# 一个人在两台电脑同步项目

GitHub 的 `main` 保存两台电脑共同的源码版本。Mac 可运行基础工作台和已有重建工具，GPU 电脑另装 Fast3R 的独立 CUDA 环境；Python 环境、工具路径和运行记录分别留在各自电脑。

## 每次切换电脑

离开当前电脑前，先查看改动，只提交本次需要同步的源码与文档：

```bash
git status
git diff
git add 这次修改的文件
git commit -m "说明本次改动"
git push origin main
```

在另一台电脑开始修改前：

```bash
git status
git pull --ff-only origin main
```

工作区干净时直接拉取。存在未提交改动时，先提交自己的工作，或 `git stash push -u` 暂存；拉取后再 `git stash pop` 并检查。`pull --ff-only` 会在两边各自产生新提交时停止，此时先提交本机改动，再 `git fetch origin`、`git rebase origin/main`，解决实际冲突后运行测试并正常推送。不要用强制推送覆盖另一台电脑的历史。

## 只有转移包、没有 Git 历史

若 `git status` 提示不是 Git 仓库，在新目录克隆已有仓库：

```bash
git clone https://github.com/shenshenovo/recon-workbench.git recon-workbench-git
cd recon-workbench-git
```

旧目录先保留，把需要的本机 `data/`、`outputs/`、`.tools/` 另行复制或迁移，再核对配置中的绝对路径。不要在转移包里重新初始化后覆盖远端主线。第一次克隆已有仓库时不需要重新 `git init` 或再次添加 `origin`。

## 哪些内容不会随 Git 同步

- `data/`：照片、SQLite 数据库、输入快照、来源清单。
- `outputs/`：点云、高斯模型、实验日志、本地 HTML 预览及完整实验报告。
- `.tools/`、`.venv/` 和 Conda 环境：第三方工具、权重和运行依赖。
- `.env`、`.private/`：本机配置和私人材料。

需要在另一台电脑演示时单独复制相应产物。当前 JSON 可能含绝对路径，仅复制数据库不能保证任务与文件仍能对应；可保留独立 HTML 预览用于离线展示。公开实测摘要保存在 [GPU_EXPERIMENTS.md](GPU_EXPERIMENTS.md)。
