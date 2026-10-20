<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="ui/assets/brand/logo-wordmark-dark.png">
    <img src="ui/assets/brand/logo-wordmark-light.png" width="420" alt="Disk Warden">
  </picture>
</p>

# Disk Warden

[English](README.md) | 简体中文

**AI IDE 存储管家**：自动识别本机 Cursor / Trae / Claude Code 等 AI 编程工具的数据目录，把缓存、索引、日志等可再生产物迁移到其他磁盘，同时让原路径继续可用，C 盘不再被吃满。

**Windows** 版已可运行；macOS 版仍在开发中。

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
| Windows 打包（T15） | 已有未签名的开发版 zip |
| macOS 打包（T21） | 未开始 |

已完成的模块由 **661 个单元测试**覆盖，CI 会在每次 push 时自动运行。

## 平台支持

- **Windows 10/11**：当前主开发与验证平台。核心引擎已实现：robocopy 搬运、`mklink /J` 建链、进程占用检测、卷信息与空间预检。
- **macOS 12+**：属于第一期产品范围（决策 0004）。平台适配层已按接口拆分，symlink 建链与进程检测骨架已就位；完整适配、Gatekeeper 首次启动引导与 `.app` 打包在 T21 完成，构建走 GitHub Actions `macos-latest`（决策 0007）。

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

桌面应用可直接用 `python -m src.app.main` 从源码启动，或使用 `packaging/build.ps1` 打出的未签名 Windows zip；`main.py` 是早期 Tkinter 试验品，仅作历史参考。

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

[PolyForm Noncommercial License 1.0.0](LICENSE)。版权所有 (c) 2026 huangtao。个人、学习、研究与非营利使用免费。**任何商业用途，包括公司内部自用，都需要单独取得商业授权**，联系 13439731205@163.com 或手机 13439731205。

该许可证属于 source-available（源码公开），不是 OSI 认可的开源许可证。

## 安全与隐私

- 迁移只复制、不递归删除源数据；删除只针对已校验的副本与失效链接
- 危险目录（IDE 安装目录、用户源码等）默认禁止迁移，黄色目录需逐项确认
- AI 分析默认关闭；开启后目录信息会发送给第三方 AI 服务，界面上会有明确提示
- 仓库根目录的 `check*.py`、`upload*.py`、`test_ssh.py`、`clean.py` 等临时脚本含敏感凭据，已被 `.gitignore` 排除，**禁止提交入库**

## 协作

- 所有变更先开 issue，完成后在 issue 评论中说明产出与风险
- 一次提交只做一件事，提交信息遵循 conventional commits
