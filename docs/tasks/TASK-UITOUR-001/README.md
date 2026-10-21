# TASK-UITOUR-001：页面截图巡检（不装包也能逐页看界面）

一条命令、一次 CI，把 `ui/index.html` 的**每个页面 × 3 个窗口宽度 × 明暗 × 中英**全部截出来，
看界面不用再装客户端、不用 Windows、不用 macOS。

- 命令：`python -m tools.uitour --out dist/uitour`
- 产物：`dist/uitour/<page>__<width>__<theme>__<lang>.png` + `index.html` 画廊 + `manifest.json`
- 默认矩阵：22 个页面 × 宽 980/1280/1600 × light/dark × zh-CN/en = **264 张图**
- 判定标准：截图过程中 console 错误数必须为 0；调用了没有 fixture 的 rpc 方法直接判失败

---

## 1. 为什么能跑起来（前提）

SPA 只在两个位置碰宿主：

1. `rpc(method, params)` 在**调用时**读 `window.pywebview.api.rpc`，返回 Promise；
2. `hostReady()` 在桥存在时立即 resolve `true`。

所以只要在文档脚本执行**之前**注入一个假的 `window.pywebview.api.rpc`，`ui/index.html`
就能在普通 Chromium 里完整跑起来 —— 不需要 pywebview、不需要 Windows、不改产品代码一行。

Playwright 的 `page.add_init_script()` 正好在这个时机执行，假桥由
`tools/uitour/fake_bridge.py` 生成：

- 按 `req.method` 返回 `tools/uitour/fixtures.py` 里的固定 fixture；
- 未知方法 **不返回空**，而是 resolve 一个 `{ok:false, error:{code:'INTERNAL', detail:{reason:'fixture missing'}}}`，
  同时 `console.error('[uitour] fixture missing for rpc method: <method>')` 并记进
  `window.__uitour.missing`；
- 冻结时钟（`Date.now()` 固定为 2026-10-21T10:00:00Z），所以设备码倒计时、历史时间都可复现；
- fixture 里的 `@now+600` / `@now-3600` 这类相对时间会按冻结时间展开。

**没写 fixture 不会静默白屏**：跑完会写在 `problems` 里、进程 exit 1，CI 直接红。

## 2. 怎么跑

```bash
# 依赖只装在开发机/CI，不随安装包走
python -m pip install -r packaging/requirements-ci.txt
python -m playwright install chromium        # 首次需要

# 全量矩阵（默认 264 张）
python -m tools.uitour --out dist/uitour
```

跑完打开 `dist/uitour/index.html` 就是带筛选（页面/宽度/主题/语言）的画廊。

常用参数：

```bash
python -m tools.uitour --list-pages                      # 看页面名
python -m tools.uitour --only ide,rules --widths 1280 \
    --themes light --langs zh-CN --out dist/uitour-1     # 改 fixture 时快速迭代
python -m tools.uitour --out dist/uitour --summary out.md # 额外追加一段 markdown 摘要
python -m tools.uitour --allow-console-errors            # 只忽略 console 错误（缺 fixture 仍失败）
```

退出码：`0` 干净；`1` 有截图出问题（console 错误 / 缺 fixture）；`2` 运行环境错误
（如没装 Playwright、浏览器起不来）。**不要用 `--allow-console-errors` 掩盖问题**，
它只用于临时排查。

### 目录与命名

```
dist/uitour/
├── index.html                                  # 画廊（自包含，可离线打开）
├── manifest.json                               # 每张图的页面/宽度/主题/语言/耗时
├── welcome__1280__light__zh-CN.png
├── ide__1600__dark__en.png
└── ...
```

文件名四段固定顺序，方便脚本比对：`<page>__<width>__<theme>__<lang>.png`。

## 3. 怎么加一个页面

页面清单在 `tools/uitour/tour.py` 的 `_pages()`，加一个 dict 就行：

```python
{
    "name": "ide-overflow",           # 文件名第一段，必须唯一
    "title": "IDE 数据 · 超长路径",    # 画廊里显示
    "group": "edge",                  # onboarding / main / edge / flow / settings
    "hash": "#ide",                   # 初始路由
    "note": "路径超过 260 字符时的省略与悬浮提示",
    "setup": "setSnapshot(%s); location.hash = '#ide'; render();"
             % _js(fixtures.snapshot()),   # 在 boot 之后执行一次
}
```

约定：

- `setup` 是**字符串形式的 JS**，在页面 boot 完成（`state.connected && !state.booting && !state.data.historyLoading`）
  之后执行一次，跑完等动画/布局稳定再截图；
- 赋值给 `location.hash` 要在 `render()` 之前，否则会先渲染旧路由、截图截到过渡态；
- 需要造数据时用 `setSnapshot(...)` / `applyAuth(...)` / 直接改 `state.data.flow`，
  不要依赖真实 RPC —— 所有数据来自 fixture；
- `_js(obj)` 是 `json.dumps` + `ensure_ascii=False` 的小工具，用来把 Python 字典塞进 JS；
- 新增页面后跑 `--only <name> --widths 1280 --themes light --langs zh-CN` 先看一张。

流程类页面（迁移/回滚）通常直接摆 `state.data.flow` 的 phase，见 `migrate_flow()` /
`rollback_flow()`；它们和真实流程共用同一个渲染分支，不需要真的跑 job。

## 4. 怎么写 fixture

全部 fixture 在 `tools/uitour/fixtures.py` 的 `fixtures()`，键就是 rpc 方法名：

```python
def fixtures():
    return {
        "scan.result": snapshot(),
        "settings.set": settings(),     # 注意：直接返回 settings 本身
        ...
    }
```

规则：

1. **以 `src/app/service.py` 的返回值为准**。fixture 不是「随便编一个形状」，而是
   「service 真的会返回什么」。方法签名、字段名、嵌套层级都要对齐，否则 SPA 会
   读到 `undefined`，截图看起来「正常」但其实少了一块。
   对应关系速查：
   - `scan.start` → `{job_id, state}`（不是 `{job}`）
   - `settings.set` / `ai.consent` → 直接返回 settings / ai 字典（不是 `{settings}`）
   - `job.status` / `scan.status` → `{job, recent_events}`
   - `plan.preview` → plan + `items/total_bytes/need_bytes/free_bytes/yellow_item_ids/...`
   - `rollback.preview` → `{job_id, plan_id, ..., requires_residual_confirm, restore_bytes}`
2. **边界样本必须覆盖**，这是这个工具存在的意义：超长路径（>260 字符）、超长项目名、
   空列表、权限不足目录（`access_errors>0`）、被进程占用（`blocked_code` + `process`）、
   已迁移项（`migrated_job_id`）、迁移进行中/完成/已回滚三种状态。
3. **相对时间用 `@now±N`**，不要写死 `"2026-10-21T10:05:00Z"` 之外还依赖当前时间；
   冻结时钟保证今天和明年跑出来一样。
4. 只加 SPA 真的会调用的方法；产品新增 rpc 时，**fixture 和 service 一起改**。
5. 加完 fixture 记得跑一次全量，确认 `0 console errors` —— 缺 fixture 或字段错位都会在这里暴露。

## 5. 已知边界（别误读截图）

| 现象 | 原因 |
| --- | --- |
| 980 宽的图右侧略挤/出现横向滚动 | 产品 `.app{min-width:1024px}`，980 这张故意用来验收最小宽度下的溢出表现 |
| dark 主题是「近似色」而不是产品真深色 | 产品目前没有 dark token，`tools/uitour/dark.css` 只是**预览用**的注入样式，不被产品引用、不进安装包 |
| 截图里没有真实扫描进度 | `job.list` fixture 故意返回空数组，避免 boot 阶段进入轮询；进度态由页面 setup 直接摆 |

`dark.css` 文件头写明了编辑规则（优先改 token，其次选择器），改完跑一张 dark 图即可验证。

## 6. CI：`.github/workflows/ui-tour.yml`

- **`shots` job**（ubuntu-latest + chromium headless）
  - 触发：`workflow_dispatch`，以及 `ui/**`、`tools/uitour/**`、`packaging/requirements-ci.txt`、
    本 workflow 文件变动时的 push / PR；
  - 先装 `fonts-noto-cjk`（否则中文全是方框），再装 CI 依赖和 chromium；
  - `python -m tools.uitour --out dist/uitour --summary "$GITHUB_STEP_SUMMARY"`，
    job summary 里直接嵌 5 张关键页缩略图（welcome / ide / rules / history / migrate-progress）；
  - artifact `ui-tour`（30 天，`if: always()` 上传，失败的图也要留证据）。
- **`mac-preview` job**（macos-latest）
  - `continue-on-error: true`，**只是尽力而为**：启动应用 + `screencapture` 抓 2~3 帧；
  - 只证明「在 macOS 上画得出来」，黑屏/失败是已知情况，不参与验收、不阻塞构建；
  - 产物 artifact `ui-tour-mac`（`if-no-files-found: ignore`）。

### 依赖隔离（硬约束）

Playwright 只在 `packaging/requirements-ci.txt`：

- **不在** `packaging/requirements-build.txt` / `requirements-build-macos.txt`；
- **不在** `packaging/DiskWarden.spec` 的 `datas` / `hiddenimports`；
- 因此安装包体积、杀软误报面都不受影响，Windows 打包行为与以前完全一致。

产品代码零改动：`ui/index.html` 没有为这个工具改过一行，假桥完全是**运行时注入**。

## 7. 文件清单

| 文件 | 作用 |
| --- | --- |
| `tools/uitour/__main__.py` | CLI：参数、退出码、`--summary` |
| `tools/uitour/tour.py` | 页面清单、矩阵、Playwright 驱动、等待与截图 |
| `tools/uitour/fixtures.py` | 所有 rpc 方法的固定返回值 + 边界样本 |
| `tools/uitour/fake_bridge.py` | 注入假 `window.pywebview`，冻结时钟，记录缺失方法 |
| `tools/uitour/server.py` | 仓库根的本地静态服务（file:// 有跨域限制，用 http 更稳） |
| `tools/uitour/gallery.py` | 画廊 `index.html`、manifest、markdown 摘要 |
| `tools/uitour/dark.css` | 预览用深色skin（产品不引用） |
| `packaging/requirements-ci.txt` | CI/开发专用依赖（仅 playwright） |
| `.github/workflows/ui-tour.yml` | 截图矩阵 job + macOS 尽力而为 job |

## 8. 已覆盖的 rpc 方法

`app.hello` · `app.ping` · `app.open_external` · `app.reveal` ·
`scan.result` · `scan.start` · `scan.status` · `scan.cancel` · `disk.info` ·
`rules.learned.list` · `rules.override` · `rules.override.clear` ·
`history.list` · `job.list` · `job.status` ·
`recovery.list` · `recovery.run` · `recovery.ignore` ·
`settings.get` · `settings.set` ·
`ai.status` · `ai.consent` · `ai.test` ·
`welcome.seen` · `auth.status` · `auth.start` · `auth.cancel` · `auth.sign_out` ·
`star.check` · `star.show` · `star.give` · `star.dismiss` ·
`plan.preview` · `migrate.start` · `rollback.preview` · `rollback.start`

新增 rpc 方法时同步补 fixture；否则第一次被调用的页面会直接失败（这是设计如此）。

## 9. 页面清单

`welcome`、`welcome-device-flow`、`star-overlay`、`projects`、`ide`、`ide-empty`、
`ide-no-tools`、`ide-scanning`、`ide-process-blocked`、`ide-yellow-confirm`、`disk`、
`rules`、`history`、`migrate-confirm`、`migrate-progress`、`migrate-success`、
`rollback-confirm`、`rollback-progress`、`rollback-success`、`settings`、`ai`、`feedback`

`python -m tools.uitour --list-pages` 可随时打印（含分组与标题）。
