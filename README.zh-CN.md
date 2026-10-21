<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="ui/assets/brand/logo-wordmark-dark.png">
    <img src="ui/assets/brand/logo-wordmark-light.png" width="420" alt="Disk Warden">
  </picture>
</p>

# Disk Warden

[English](README.md) | 简体中文

**AI IDE 存储管家**：自动识别本机 Cursor / Trae / Claude Code 等 AI 编程工具的数据目录，把缓存、索引、日志等可再生产物迁移到其他磁盘，同时让原路径继续可用，C 盘不再被吃满。

**Windows** 版已可运行；**macOS**（11+，Apple Silicon）版由 CI 构建发布未签名包，首次打开步骤见 [macOS 首次启动](#macos-首次启动)。

## 下载与安装

不用装 Python，也不用自己编译：打开 [**Releases 发布页**](https://github.com/QuotaFuse-ai/disk-warden/releases/latest)，下载对应平台的压缩包即可。每个 zip 里就是完整程序，解压即用。

| 平台 | 文件 | 怎么用 |
|---|---|---|
| Windows 10 / 11 x64 | `DiskWarden-0.9.0-beta.9-win-x64.zip` | 解压到固定目录，双击 `DiskWarden\DiskWarden.exe`。 |
| macOS 11+（Apple Silicon） | `DiskWarden-0.9.0-beta.9-macos-arm64.zip` | 解压后打开 `DiskWarden.app`，首次需要放行一次，见 [macOS 首次启动](#macos-首次启动)。 |

每个压缩包都附带同名的 `.sha256` 校验文件和 zip 根目录下的 `START-HERE*.txt` 使用说明。

**验证到什么程度**

- **Windows**：在 Windows 10/11 上实际构建并跑过——完整单元测试、打包产物的沙箱演练、冻结后的自检，全部针对发布出去的这一个包执行。
- **macOS**：**目前还没有在真机上测试过**。团队手上没有 Mac，`.app` 由 GitHub Actions 的 `macos-latest` 构建，只通过了自动化的 `--smoke` 自检（包结构、WKWebView 后端、UI 文件是否齐全）。请当作开发者预览版看待：打包链路已验证，点击操作未验证。发现问题欢迎提 issue。

两个包都**未做代码签名**：Windows 首次运行会弹 SmartScreen（点「更多信息」→「仍要运行」），macOS 首次打开会被 Gatekeeper 拦截一次。这两个提示都不是病毒告警，也不需要关闭任何系统安全功能。

界面默认是**英文**；点标题栏的 `中文` / `EN` 按钮可切换为简体中文，选择会被记住。

## 仓库安全说明

现在不需要克隆源码，从 Releases 下载压缩包即可，但仍可自行验证：

- 每个压缩包旁边都有 `.sha256` 文件。运行前先比对 `Get-FileHash -Algorithm SHA256`（PowerShell）或 `shasum -a 256` 的结果。
- 仓库里**不存放任何编译好的可执行文件**。用户运行的每个二进制都由 `packaging/build.ps1` 或 macOS CI 从打标签的提交构建，再挂到 Release 上。
- `python tools/repo_safety_check.py` 可离线审计仓库：提交进仓库的凭据、下载即执行的脚本、混淆命令、针对根目录的递归删除、可疑外连域名、符号链接、伪装扩展名、压缩包路径穿越。它不执行任何代码，也不上传任何数据。
- 最近一次结果见 [`docs/security/repo-safety-report.md`](docs/security/repo-safety-report.md)，完整说明（应用会读写什么、如何验证下载、漏洞上报）见 [`SECURITY.md`](SECURITY.md)。

## 下载与安装

不用装 Python，也不用自己编译：打开 [**Releases 发布页**](https://github.com/QuotaFuse-ai/disk-warden/releases/latest)，下载对应平台的压缩包即可。每个 zip 里就是完整程序，解压即用。

| 平台 | 文件 | 怎么用 |
|---|---|---|
| Windows 10 / 11 x64 | `DiskWarden-0.9.0-beta.8-win-x64.zip` | 解压到固定目录，双击 `DiskWarden\DiskWarden.exe`。 |
| macOS 11+（Apple Silicon） | `DiskWarden-0.9.0-beta.8-macos-arm64.zip` | 解压后打开 `DiskWarden.app`，首次需要放行一次（Gatekeeper）。 |

每个压缩包都附带同名的 `.sha256` 校验文件和 zip 根目录下的 `START-HERE*.txt` 使用说明。

- **Windows**：在 Windows 10/11 上实际构建并跑过——完整单元测试、打包产物的沙箱演练、冻结后的自检，全部针对发布出去的这一个包执行。
- **macOS**：**目前还没有在真机上测试过**。团队手上没有 Mac，`.app` 由 GitHub Actions 的 `macos-latest` 构建，只通过了自动化的 `--smoke` 自检（包结构、WKWebView 后端、UI 文件是否齐全）。请当作开发者预览版看待，发现问题欢迎提 issue。

两个包都**未做代码签名**，首次运行会分别出现一次 SmartScreen（Windows，「更多信息」→「仍要运行」）或 Gatekeeper 提示（macOS）。这两个提示都不是病毒告警，也不需要关闭任何系统安全功能。

## 与其他清理工具的区别

- 认识 AI IDE 的目录结构，区分「可迁」「不可动」「未知」三类，而不是按文件类型乱删
- 迁移后原路径变成链接（Windows 用 junction，macOS 用 symlink），IDE 与命令行无感知
- 迁移前备份、迁移后读回校验；任何一步失败都会回滚，源数据始终至少保留一份
- 已迁移目录可一键迁回原位置；异常中断的迁移在下次启动时自动恢复
- 可选的联网 AI 分析：只针对未知目录，默认关闭

## 当前状态

| 能力 | 状态 |
|---|---|
| 目录扫描、体积统计、风险判定（T06–T08） | 已完成 |
| 数据搬运、校验、建链、崩溃恢复（T09–T10） | 已完成 |
| 回滚引擎与本地存储：journal / 历史 / 设置 / 学习规则（T11） | 已完成，已合入 `main` |
| AI 分析接口（T22） | 已完成，默认关闭 |
| 界面接入真实引擎、AI 密钥配置（T12、T25） | 已完成，已合入 `main` |
| 欢迎页与 GitHub 登录、Star 引导（T16） | 开发中：GitHub Device Flow；未登录也不限制任何本地功能 |
| Windows 打包（T15） | 已发布 `DiskWarden-0.9.0-beta.9-win-x64.zip`：托盘、当前用户自启、日志轮转、打包产物沙箱演练 |
| macOS 平台适配与打包（T21） | 已发布 `DiskWarden-0.9.0-beta.9-macos-arm64.zip`，**未在真机测试**：`.app` 由 GitHub Actions `macos-latest` 构建，rsync/cp 搬运、`ln -s` 建链与回读、LaunchAgent 自启、托盘、日志轮转、应用内首次启动引导；CI 构建并对产物做冒烟检查 |

已完成的模块由 **934 个单元测试**覆盖，CI 会在每次 push 时自动运行。

## 平台支持

- **Windows 10/11**：当前主开发与验证平台。核心引擎已实现：robocopy 搬运、`mklink /J` 建链、进程占用检测、卷信息与空间预检。
- **macOS 11+（Apple Silicon）**：属于第一期产品范围（决策 0004）。所有平台相关实现都在 `src/platform/mac/`：`rsync`/`cp` 搬运、`ln -s` 建链并回读目标、卷信息与空间检查、进程检测、当前用户 LaunchAgent 自启。未签名 `.app` 由 GitHub Actions `macos-latest` 构建（决策 0007）；Intel Mac 需要单独的 x64 构建，目前尚未产出。
  - **0.9.0-beta 的已知缺口**：自动发现（`data/ide-catalog.json` + `src/core/scan/discovery.py`）仍是 Windows 专用，在 Mac 上扫描结果为 0，列表是空的。应用、托盘、自启、日志与迁移引擎均就绪；macOS 目录清单是后续任务。

## macOS 首次启动

0.9.0-beta 的 macOS 包**未做代码签名与公证**，Gatekeeper 会拦截第一次打开。根治办法是签名 + 公证，是否投入由人类后续决定。在那之前：

1. 把整个压缩包解压到固定目录，打开 `DiskWarden.app`（访达里显示为 “Disk Warden”）。
2. 按系统版本放行：
   - **macOS 15（Sequoia）及更新**：右键 → 打开已经不能绕过拦截。先双击触发提示并关掉，再进入 **系统设置 → 隐私与安全性**，滚动到「安全性」，对 Disk Warden 点 **仍要打开**，验证密码后再次确认。
   - **macOS 13（Ventura）~ 14（Sonoma）**：按住 Control 点按（或右键）应用 → **打开**，在提示框里再点一次 **打开**。
3. 应用首次启动时会自动弹出同样的步骤；之后可在 **设置 → 首次启动说明** 随时重看。

以上步骤都无效时，高级用户可在终端执行 `xattr -dr com.apple.quarantine "/Applications/Disk Warden.app"`。它会去掉整个 App 的隔离标记，等于放弃对该应用的来源检查，并不能替代签名；只有在确认压缩包来自官方发布页时才使用。同一份说明随包放在 zip 根目录的 `START-HERE-macOS.txt`（源文件 `packaging/START-HERE-macOS.txt`）。

数据目录为 `~/Library/Application Support/DiskWarden`（日志在下面的 `logs/app.log`）；自启写入 `~/Library/LaunchAgents/com.quotafuse.diskwarden.plist`，下次登录生效。

## 技术栈

- Python 3.12 为正式构建工具链（决策 0012）；CI 在 3.10 与 3.12 上跑测试
- 界面：原生 HTML/JS 单页（`ui/index.html`），由 pywebview 承载（Windows WebView2 / macOS WKWebView）
- 打包：PyInstaller 6 onedir；macOS 产物由 CI 构建

## 开发

环境要求：Python 3.10 及以上。

```bash
# 全量单元测试
python -m unittest discover -s tests -q

# 迁移演练：仅在系统临时目录中造数，不触碰真实用户数据
python tools/migrate_drill.py
python tools/migrate_drill_b.py

# 查看界面原型：静态页面，数据为示例
# 直接用浏览器打开 ui/index.html
```

桌面应用可直接用 `python -m src.app.main` 从源码启动，或使用 `packaging/build.ps1` 打出的未签名 Windows zip；macOS 的 `.app` 由 `packaging/build-macos.sh`（在 Mac 上）或 CI 的 `build-macos` workflow 构建；`main.py` 是早期 Tkinter 试验品，仅作历史参考。

## 目录结构

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

## 文档索引

1. `docs/功能需求.md` — 产品需求
2. `docs/目录清单.md` — 规则引擎的数据基础
3. `docs/specs/产品规格-v1.0.md` — 验收口径与安全不变量
4. `docs/specs/架构设计-v1.0.md` — 模块、接口与数据流
5. `docs/specs/界面交互说明-v1.0.md` — 页面与交互规则

## 许可证

[PolyForm Noncommercial License 1.0.0](LICENSE)。版权所有 (c) 2026 huangtao。个人、学习、研究与非营利使用免费。**任何商业用途，包括公司内部自用，都需要单独取得商业授权**，联系 44062290+QuotaFuse-ai@users.noreply.github.com。

该许可证属于 source-available（源码公开），不是 OSI 认可的开源许可证。

## 安全与隐私

- 迁移只复制、不递归删除源数据；删除只针对已校验的副本与失效链接
- 危险目录（IDE 安装目录、用户源码等）默认禁止迁移，黄色目录需逐项确认
- AI 分析默认关闭；开启后目录信息会发送给第三方 AI 服务，界面上会有明确提示
- 仓库根目录的 `check*.py`、`upload*.py`、`test_ssh.py`、`clean.py` 等临时脚本含敏感凭据，已被 `.gitignore` 排除，**禁止提交入库**

## 协作

- 所有变更先开 issue，完成后在 issue 评论中说明产出与风险
- 一次提交只做一件事，提交信息遵循 conventional commits
