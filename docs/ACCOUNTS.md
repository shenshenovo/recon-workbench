# 账号、权限与模型下载

对应 [后续任务分工](TEAM_TASK_PLAN.md) 中"石孟千"一项：实现注册、登录、退出；为项目、图片、任务和模型增加用户归属与权限；增加模型下载。交付要求是两个账号的数据相互隔离、图片/日志/点云/下载接口都检查权限、已有项目和历史记录仍可使用。

## 1. 技术架构

本项目**不使用 FastAPI、Flask 或任何 Web 框架**，也没有前端构建工具。全部基于 Python 标准库 + 原生浏览器 API，唯一三方依赖是 Pillow（图片检测）。

| 层 | 实现 | 文件 |
| --- | --- | --- |
| HTTP 服务 | `ThreadingHTTPServer` + `BaseHTTPRequestHandler`，显式路由表，JSON 手写序列化 | `recon_workbench/server.py` |
| 账号基础件 | 口令散列、会话令牌、用户名/密码规则、登录限流 | `recon_workbench/accounts.py` |
| 会话与授权 | Cookie 读写、`Access` 授权上下文、注册/登录/退出编排 | `recon_workbench/access.py` |
| 持久化 | SQLite；只负责读写，列表查询接受 `owner` 过滤 | `recon_workbench/workspace.py` |
| 共用异常 | `AccessDenied`（越权 → 403） | `recon_workbench/errors.py` |
| 任务进程 | `subprocess.Popen` 启 `main.py`，后台线程回写状态 | `server.py::Jobs` |
| 前端 | 4 个 HTML + 原生 JS，服务端注入本次启动令牌 | `web/` |

分层规则（改动时遵守）：

1. `accounts` 不导入数据库、不导入 HTTP，只做纯计算与限流。
2. `workspace` 不判断权限，只按传入的 `owner` 过滤；越权由上游抛 `AccessDenied`。
3. `access` 是唯一回答"谁能看什么"的地方，也是按账号收窄的数据视图。
4. `server` 只做路由、请求解析、文件响应与进程控制，不写 SQL、不比对口令。

一次 GET 请求的顺序：

```
Host 校验 → 解析 Cookie 得 Access → 页面/静态资源放行或 302 到 /login
        → /api/* 且账号体系已启用而未登录 → 401
        → read_route() 用 Access 读数据 → AccessDenied → 403 / ValueError → 400
```

## 2. 数据模型

```text
users(id, username UNIQUE, password_hash, created_at, last_login_at)
sessions(token_hash PK, user_id → users.id, created_at, expires_at)
projects(..., owner_id)        ← 唯一真实归属
image_sets(project_id)  ─┐
tasks(project_id)        ─┼─ 经项目继承归属
models(task_id)         ─┘
```

归属只存 `projects.owner_id` 一列：图片集属于项目、任务属于项目、模型属于任务。因此不需要在四张表上重复写 owner，也不存在"三处不同步"。鉴权沿这条链判断，例如下载模型时走 `models → tasks → projects`。

口令与令牌：

- 口令用 PBKDF2-HMAC-SHA256，210000 次迭代，16 字节随机盐，存为 `pbkdf2_sha256$迭代数$盐$摘要`；不存明文、不可逆。校验用 `hmac.compare_digest` 常量时间比较。
- 会话令牌 32 字节随机串（`secrets.token_urlsafe`），**数据库只存 SHA-256 摘要**；泄露数据库也无法还原可用令牌。
- Cookie 为 `rw_session`，`HttpOnly` + `SameSite=Strict` + 30 天 `Max-Age`。回环地址走 http，因此不加 `Secure`，否则浏览器不会回传。
- 会话过期即在查询时清除；`/api/auth/logout` 删除记录并下发 `Max-Age=0`。

## 3. 权限矩阵

所有返回数据的接口都经过 `Access`，越权统一 403，参数不合法 400，未登录 401，登录失败次数超限 429。

| 接口 | 校验方式 | 越权结果 |
| --- | --- | --- |
| `GET /api/projects` | 只列 `owner_id` 等于当前账号的项目 | — |
| `GET /api/project` | `project(id, owner)` | 403 |
| `GET /api/tasks`、`/api/runs` | 按账号项目过滤，SQL 层 WHERE 下推 | 403（显式传别人项目时） |
| `GET /api/task` | `task(id, owner)`，含日志与阶段 | 403 |
| `GET /api/quality`、`POST /api/quality-analyze`、`POST /api/image-selection` | 经 `image_set → project` | 403 |
| `GET /api/source-image` | 经图片集归属 | 403 |
| `GET /api/image`、`/api/previews` | 路径必须落在本账号任务的结果目录内 | 403 |
| `GET /api/cloud` | 任务必须在账号的已完成列表内 | 400「未知任务」 |
| `GET /api/model` | `models → tasks → projects`，且文件确实在任务目录内 | 403 |
| `GET /api/job`、`POST /api/cancel` | 当前任务槽位归属校验；别人的任务返回 idle，不能取消 | 403（cancel） |
| `POST /api/upload-file` | 上传批次带归属，跨账号复用批次 | 403 |
| `POST /api/start`、`/api/report`、`/api/open` | 项目/图片集/任务归属校验 | 403 |
| 页面 `/`、`/reconstruct`、`/projects`、`/experiments` | 已启用账号而未登录 | 302 到 `/login?next=` |

不做的越界检查：`/api/folders` 只列目录名，用于选择"项目内输入目录"，输入目录本身是共享数据集，不做归属划分；这一点在限制里说明。

## 4. 历史数据与兼容模式

数据库增加 `users`、`sessions` 两张表，并用 `ALTER TABLE projects ADD COLUMN owner_id` 扩展已有库（`CREATE TABLE IF NOT EXISTS` + `PRAGMA table_info` 判断，不删原有记录）。`owner_id IS NULL` 表示"账号体系建立之前的记录"。

- **首个注册账号一次性接管**全部 `owner_id IS NULL` 的项目与历史任务，`default` 项目连同 `outputs/` 里导入的历史任务原样可打开。注册接口返回 `claimed_projects` 数量，页面提示接管了多少个。
- **之后注册的账号**各自独立，自动得到一个「我的项目」起始项目。
- **兼容模式**：数据库中还没有任何账号时，保持原有单机行为（匿名可用，见到的就是历史数据范围），老用户不注册也能继续用。一旦存在账号，其余接口一律要求登录。
- 服务启动时仍会 `import_history()` 把 `outputs/` 下未登记的历史任务补进 `default` 项目；之后注册的账号看不到它们（已归属首个账号）。

## 5. 模型下载

`GET /api/model?id=<模型记录 ID>`：

1. 经 `models → tasks → projects` 校验归属，模型记录不存在或不属于当前账号 → 403。
2. 解析出磁盘路径后再次校验它仍在所属任务的 output 目录内（防路径穿越），文件不存在 → 400。
3. 以 `1 MiB` 分块流式回写，带 `Content-Length`、`Content-Disposition: attachment; filename*=UTF-8''…`，`Cache-Control: no-store`，不把大 PLY 读进内存。

页面入口：项目记录 →「模型记录」页签，每个文件一行「名称 · 大小 · 路径 + 下载」；任务详情对话框新增「模型文件与下载」区块。

## 6. 测试

```bash
python -m unittest tests.test_accounts    # 新增 15 项
python -m unittest                        # 全量
```

`tests/test_accounts.py` 分两层：`AccountTests` 直接验证散列/盐、令牌只存摘要、过期会话、历史接管、双账号数据视图、登录限流；`IsolationApiTests` 用真实 HTTP 与 Cookie 验证 401/403/302、注册登录退出闭环、跨账号访问图片/任务/模型/点云/预览/上传批次、历史任务仍可打开、下载文件被删除后的报错。

## 7. 实际运行记录

2026-11-01，Windows + Python 3.13.14，本机 `python app.py --no-browser --port 8791` 真实进程，用 curl 走完整流程（库里只放了 1 个 `outputs/demo-legacy` 历史任务夹具，用于验证历史导入与接管；本机未安装 COLMAP，未做真实重建）：

```text
1  无账号时 /api/auth/me        {"user": null, "auth_required": false, "user_count": 0}
2  未登录访问 /reconstruct      200（此时账号体系未启用）
3  注册首个账号 石孟千           claimed_projects=1，项目 ['默认项目 / 历史实验']
4  该账号可见历史任务            [('demo-legacy', 'completed', 'mvs', 1 个模型)]
5  下载模型                     HTTP 200 · Content-Disposition: attachment; filename*=UTF-8''sparse.ply
                               Content-Length: 31 · 内容与源文件一致
6  已有账号后未登录             /reconstruct → 302 /login?next=%2Freconstruct；/api/projects → 401
7  注册第二个账号 李四           claimed_projects=0，项目 ['我的项目']
8  第二个账号的可见范围          项目 ['我的项目']，任务 []
9  越权访问第一个账号的资源       project → 403 · task → 403 · model → 403 · runs → 403
10 第一个账号退出                {"ok": true, "removed": true}，随后 /api/projects → 401
11 重新登录                      projects → 200
12 错误口令                      {"error": "用户名或密码错误。"} 401
13 弱密码 / 重名注册             400「密码需要 8～128 个字符。」/ 400「该用户名已被使用。」
14 上传两张照片                  1/2 张写入成功
15 原图读取权限                  本人 200 · 他人 403
16 他人复用上传批次              {"error": "上传批次不属于当前账号。"} 403
17 质量检测接口                  本人 200 · 他人 403「图片集不存在或无权访问。」
18 连续 8 次错误口令后第 9 次       {"error": "登录尝试次数过多，请 60 秒后再试。"} 429（口令正确也被拦）
```

全量测试（含既有用例）：

```text
Ran 92 tests in 23.5s
FAILED (failures=2, errors=2, skipped=3)
```

本机 Windows 上失败的 4 项全部是既有用例的平台假设，与本改动无关，macOS 上仍按原样通过：

| 用例 | 原因 |
| --- | --- |
| `test_main.test_existing_inputs_skip_sfm` | 断言里写死 POSIX 路径 `/tmp/source` |
| `test_workspace.test_recover_actual_completed_output_and_models` | 断言用 `/` 拼接，Windows 返回 `\` |
| `test_image_quality.test_snapshot_copies_exact_selection_and_rejects_changed_source` | 报告里的相对名在 Windows 带 `\`，用 `nested/copy.png` 取不到 |
| `test_prepare_brush.test_paths_cannot_escape_image_root` | 创建符号链接需要管理员权限或开发者模式 |

另外为在本机跑通测试，`server.py` 做了两处平台适配（POSIX 行为不变）：`fcntl` 缺失时用 `msvcrt.locking` 实现同一份"同目录只允许一个服务"的锁；`os.killpg` 缺失时用 `process.terminate()` 停止子进程（Windows 发 `CTRL_BREAK_EVENT` 会打到本进程所在控制台，连带结束服务）。冒烟期间第 1～17 步与锁路径均在 Windows 上实跑通过，服务正常启动与退出。

## 8. 已知限制

- 只在 `127.0.0.1` 提供服务，权限用于区分本机不同使用者，不是互联网多租户方案；仍然依赖 `Host` 校验与 `X-Local-Token` 防跨站请求。
- 忘记密码没有自助重置，需要直接改 `data/workbench.sqlite3` 或删掉 `users` 行重新注册。
- 登录限流按用户名在内存中计数（默认 8 次失败锁 60 秒，返回 429），重启服务即清空；多进程部署时各自计数。
- 注册开放：任何能访问本机页面的人都能新建账号，但只能看到自己的数据。
- 输入目录浏览（`/api/folders`）与共享数据集不做归属划分；`data/` 下的历史输入目录对已登录用户可见目录名。
- 模型下载是同步流式响应，没有断点续传（Range）与下载审计日志。

## 9. 合并到 main

当前分支 `smq_bk`，基于 `main@127c485`，三个提交：

| 提交 | 内容 |
| --- | --- |
| `96a1d4f` | 主体：账号、权限、模型下载、前端与测试 |
| `5e79ef2` | 上传接口先校验登录态 + 对应测试 |
| `f341b7a` | `Access` 归属语义注释 |

`git rev-list --left-right --count origin/main...smq_bk` 为 `0 3`，此刻可快进合并。队友提交后按下列流程再走一遍：

```bash
git fetch origin
git rebase origin/main          # 只在自己分支上 rebase，不在 main 上 rebase
python -m unittest tests.test_accounts
python -m unittest               # Windows 上仍有 4 项既有的平台失败，见第 7 节
git push -u origin smq_bk        # 开 PR 合入 main
```

### 冲突热点

本分支改动集中在四个共享文件，正好也是其他人的交叉修改点。冲突时按"保留归属参数 + 复用 `Access`"的原则解，不要退回旧写法。

| 文件 | 其他人的改动 | 本分支的改动 | 解决原则 |
| --- | --- | --- | --- |
| `recon_workbench/server.py` | 张鑫：任务队列、并行调度、阶段恢复 | 提出 `PAGES`/`ASSETS` 常量；新增 `prepare()`/`require_login()`/`job_visible()`；`read_route()`/`write_route()` 拆分；`download_model()`、`send_file()` | `Jobs.start(payload, owner=ANY_OWNER)` 的 owner 参数必须保留并透传给队列记录；新队列接口（取消/查询/恢复）同样要按 owner 过滤，否则任何账号都能看或停别人的任务 |
| `recon_workbench/workspace.py` | 张鑫：tasks 字段与状态；林仪：quality 相关表 | `users`/`sessions` 建表、`projects.owner_id` 迁移、各查询的 `owner=` 形参 | 迁移代码必须保留（`PRAGMA table_info` + `ALTER TABLE`），新建查询若返回项目/图片集/任务/模型，一律接受 `owner=ANY_OWNER` 并下推到 SQL |
| `docs/COLLABORATION.md` | 全组都改 | 模块表三行、HTTP 表五个接口、权限约定段 | 按小节合并；"账号与权限约定"整段不要删，新接口要补进同一张表 |
| `web/index.html` | 周政炀：重建方法选择区 | 只改顶栏 | 顶栏 `#user-chip` / `#logout` 必须保留，`common.js` 依赖这两个 id |
| `web/styles.css` | 周政炀、林仪都会追加样式 | 追加 `topbar-right`/`user-chip`/`auth-*`/`model-row` | 纯追加，冲突时两边都留 |

无冲突的部分：`accounts.py`、`access.py`、`errors.py`、`web/login.*`、`tests/test_accounts.py`、`docs/ACCOUNTS.md` 都是新文件。

### 合并后的必查项

别人先合并时，rebase 后至少手工确认三件事（自动化测试已覆盖，但要确认没被静默改掉）：

1. 未登录访问 `/reconstruct` 仍跳 `/login`，`/api/*` 仍返回 401。
2. 第二个账号仍看不到第一个账号的项目、任务、模型（`/api/project`、`/api/task`、`/api/model` 都 403）。
3. `/api/model` 仍返回 `Content-Disposition: attachment`，且路径不在任务目录内时报错。

### PR 描述模板

```text
标题：实现账号、按账号隔离项目/图片/任务/模型，新增模型下载

改动：
- accounts.py 口令散列（PBKDF2）、会话令牌、凭据规则、登录限流（429）
- access.py 会话解析 + Access 授权上下文，统一收口按归属的读操作
- errors.py AccessDenied(403) / RateLimited(429)
- workspace.py users/sessions 表、projects.owner_id 迁移、查询按 owner 过滤
- server.py /api/auth/{register,login,logout,me}、401/302 拦截、/api/model 分块下载
- web 登录页 + 顶栏用户区 + 模型下载入口

验证：
- 新增 tests/test_accounts.py 16 项，全部通过
- 全量 python -m unittest：92 项，4 项为 Windows 既有平台失败（与本次无关）
- 真实进程 18 步冒烟记录见 docs/ACCOUNTS.md 第 7 节

影响：
- 数据库自动迁移，已有项目与历史任务由首个注册账号接管，未注册时保持原行为
- 新增依赖：无（仍只有 Pillow）
```
