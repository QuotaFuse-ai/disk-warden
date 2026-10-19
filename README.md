# Disk Warden

[中文](#中文文档) · [English](#english)

---

## 中文文档

**AI IDE 存储管家**：自动识别本机 Cursor / Trae / Claude Code 等 AI 编程工具的数据目录，把缓存、索引、日志等可再生产物迁移到其他磁盘，同时让原路径继续可用，C 盘不再被吃满。

支持 **Windows 与 macOS** 双平台。

### 与其他清理工具的区别

- 认识 AI IDE 的目录结构，区分「可迁」「不可动」「未知」三类，而不是按文件类型乱删
- 迁移后原路径变成链接（Windows 用 junction，macOS 用 symlink），IDE 与命令行无感知
- 迁移前备份、迁移后读回校验；任何一步失败都会回滚，源数据始终至少保留一份
- 已迁移目录可一键迁回原位置；异常中断的迁移在下次启动时自动恢复
- 可选的联网 AI 分析：只针对未知目录，默认关闭

### 当前状态

| 能力 | 状态 |
|---|---|
| 目录扫描、体积统计、风险判定（T06–T08） | 已完成 |
| 数据搬运、校验、建链、崩溃恢复（T09–T10） | 已完成 |
| 回滚引擎与本地存储：journal / 历史 / 设置 / 学习规则（T11） | 已完成，已合入 `main` |
| AI 分析接口（T22） | 已完成，默认关闭 |
| 界面接入真实引擎（T12） | 进行中 |
| Windows / macOS 打包（T15 / T21） | 未开始 |

已完成的模块由 **437 个单元测试**覆盖。

**平台支持**

- **Windows 10/11**：当前主开发与验证平台。核心引擎已实现：robocopy 搬运、`mklink /J` 建链、进程占用检测、卷信息与空间预检。
- **macOS 12+**：属于第一期产品范围（决策 0004）。平台适配层已按接口拆分，symlink 建链与进程检测骨架已就位；完整适配、Gatekeeper 首次启动引导与 `.app` 打包在 T21 完成，构建走 GitHub Actions `macos-latest`（决策 0007）。

### 技术栈

- Python 3.8+ 为代码基线，正式发布基线为 3.12（决策 0005）
- 界面：原生 HTML/JS 单页（`ui/index.html`），由 pywebview 承载（Windows WebView2 / macOS WKWebView）
- 打包：PyInstaller onedir；macOS 产物由 CI 构建

### 开发

环境要求：Python 3.8 及以上。

```bash
# 全量单元测试
python -m unittest discover -s tests -q

# 迁移演练：仅在系统临时目录中造数，不触碰真实用户数据
python tools/migrate_drill.py
python tools/migrate_drill_b.py

# 查看界面原型：静态页面，数据为示例
# 直接用浏览器打开 ui/index.html
```

桌面应用本体（真实数据 + 桌面外壳）在 T12、T15 完成后可运行；`main.py` 是早期 Tkinter 试验品，已弃用，仅作历史参考。

### 目录结构

```
src/
  core/scan/       扫描与目录枚举
  core/rules/      规则引擎与风险分级
  core/migrate/    迁移：备份、搬运、校验、建链、崩溃恢复
  core/rollback/   回滚：预检、执行、恢复
  core/store/      本地存储：journal、迁移历史、设置、学习规则、日志
  core/ai/         AI 分析接口（默认关闭）
  platform/        平台适配层：win / mac / posix
data/              IDE 目录清单与规则数据
ui/                静态界面原型
docs/              需求、决策、规格、任务产出
tests/             单元测试
tools/             演练与校验脚本
```

### 文档索引

1. `docs/tasks/TASK-001/项目现状总结.md` — 资产盘点、完成度、风险清单
2. `docs/decisions/` — 决策记录，只增不改
3. `docs/功能需求.md` — 产品需求
4. `docs/目录清单.md` — 规则引擎的数据基础
5. `docs/dev/协作规范.md` — 目录归属、分支与提交规则

### 安全与隐私

- 迁移只复制、不递归删除源数据；删除只针对已校验的副本与失效链接
- 危险目录（IDE 安装目录、用户源码等）默认禁止迁移，黄色目录需逐项确认
- AI 分析默认关闭；开启后目录信息会发送给第三方 AI 服务，界面上会有明确提示
- 仓库根目录的 `check*.py`、`upload*.py`、`test_ssh.py`、`clean.py` 等临时脚本含敏感凭据，已被 `.gitignore` 排除，**禁止提交入库**

### 协作

- 所有变更以 Multica issue 为入口，完成后在 issue 评论中说明产出与风险
- 分支命名、提交信息、目录归属见 `docs/dev/协作规范.md`

---

## English

**Disk Warden** is a storage manager for AI coding tools. It finds the data directories of Cursor, Trae, Claude Code and other AI IDEs on your machine, moves the reproducible bulk (caches, indexes, logs) to another drive, and keeps the original paths working — so your system drive stops filling up.

Runs on **Windows and macOS**.

### Why not just a cleanup tool

- It understands AI IDE directory layouts and classifies each directory as **movable**, **do-not-touch**, or **unknown** — instead of deleting files by extension
- After a move, the original path becomes a link (junction on Windows, symlink on macOS); IDEs and shells keep working unchanged
- Every move is backed up first and verified afterwards; any failure rolls back, and the source data is never the only copy at risk
- Migrated directories can be rolled back with one click; interrupted migrations are recovered automatically on next start
- Optional online AI analysis for unknown directories only — **off by default**

### Status

| Capability | Status |
|---|---|
| Directory scan, size accounting, risk classification (T06–T08) | Done |
| Copy, verification, link creation, crash recovery (T09–T10) | Done |
| Rollback engine and local store: journal / history / settings / learned rules (T11) | Done, merged to `main` |
| AI analysis interface (T22) | Done, disabled by default |
| UI wired to the real engine (T12) | In progress |
| Windows / macOS packaging (T15 / T21) | Not started |

**437 unit tests** cover the completed modules.

**Platforms**

- **Windows 10/11** — primary development and verification platform. The core engine is implemented: robocopy transfer, `mklink /J` links, process-in-use detection, volume and free-space checks.
- **macOS 12+** — in scope for the first release (decision 0004). The platform adapter layer is split by interface, with symlink and process-detection scaffolding in place; full adaptation, Gatekeeper first-launch guidance and `.app` packaging land in T21, built on GitHub Actions `macos-latest` (decision 0007).

### Stack

- Python 3.8+ as the code baseline; 3.12 for official builds (decision 0005)
- UI: a single self-contained HTML/JS page (`ui/index.html`) hosted by pywebview (WebView2 on Windows, WKWebView on macOS)
- Packaging: PyInstaller onedir; macOS artifacts built in CI

### Development

Requires Python 3.8 or newer.

```bash
# Full unit test suite
python -m unittest discover -s tests -q

# Migration drills: sandboxed in the system temp directory, never touch real user data
python tools/migrate_drill.py
python tools/migrate_drill_b.py

# View the UI prototype: static page with sample data
# Open ui/index.html in a browser
```

The desktop app itself (real data + native shell) becomes runnable after T12 and T15. `main.py` is a deprecated Tkinter experiment kept for reference only.

### Layout

```
src/
  core/scan/       directory scanning and enumeration
  core/rules/      rule engine and risk classification
  core/migrate/    migration: backup, copy, verify, link, crash recovery
  core/rollback/   rollback: preflight, execution, recovery
  core/store/      local store: journal, history, settings, learned rules, logs
  core/ai/         AI analysis interface (off by default)
  platform/        platform adapters: win / mac / posix
data/              IDE catalog and rule data
ui/                static UI prototype
docs/              requirements, decisions, specs, task outputs
tests/             unit tests
tools/             drills and validation scripts
```

### Documentation

1. `docs/tasks/TASK-001/项目现状总结.md` — asset inventory, completeness, risks
2. `docs/decisions/` — decision records (append-only)
3. `docs/功能需求.md` — product requirements
4. `docs/目录清单.md` — data behind the rule engine
5. `docs/dev/协作规范.md` — ownership, branching and commit rules

### Safety and privacy

- Migration copies only; the source is never recursively deleted. Deletion targets verified copies and stale links only
- Dangerous directories (IDE installations, user source code) are blocked from migration; yellow-tier directories require explicit confirmation
- AI analysis is off by default. When enabled, directory information is sent to a third-party AI service, and the UI says so before you turn it on
- Temporary scripts in the repo root (`check*.py`, `upload*.py`, `test_ssh.py`, `clean.py`) contain sensitive credentials. They are excluded by `.gitignore` and **must not be committed**

### Contributing

- All changes start from a Multica issue; report outputs and risks in the issue comment when done
- Branching, commit messages and directory ownership: `docs/dev/协作规范.md`
