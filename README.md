# Disk Warden

AI IDE 存储管家：自动识别本机 Cursor / Trae / Claude Code 等 AI 编程工具的数据目录，把缓存、索引、日志等可再生产物迁移（junction / symlink）到其他盘，让 C 盘不再被吃满。

与其他清理工具的区别：

- 认识 AI IDE 的目录结构，区分「可迁」「不可动」「未知」三类
- 迁移后通过 junction（Windows）或 symlink（macOS）让新数据继续写入目标盘
- 迁移前备份配置、迁移后校验，可一键回滚

## 当前状态

**项目处于「文档完成、代码未开始」阶段，核心链路尚不可运行。**

| 资产 | 位置 | 状态 |
|---|---|---|
| 功能需求 | `docs/功能需求.md` | 可用（v0.1） |
| IDE 目录清单 | `docs/目录清单.md` | 可用 |
| 竞品与事故调研 | `reference/社区问题与竞品调研.md` | 可用 |
| 界面原型 | `ui/index.html` | 静态原型，数据全部写死 |
| 桌面试验程序 | `main.py` | 弃用的 Tkinter 试验品，与需求文档技术方案不一致 |
| 扫描 / 规则 / 迁移 / 回滚 / 存储 | — | 均未实现 |

技术栈以 `docs/功能需求.md` 第 4 章为参考（Python 3.12 + 原生 HTML/JS + PyInstaller），但运行时外壳选型尚未拍板，见 `docs/decisions/0002-运行时外壳选型待确认.md`。在选型确认前，只应产出栈无关内容。

## 目录结构

```
.
├── README.md                 # 本文件
├── .gitignore                # 提交白名单的反面：产物、截图、密钥脚本一律忽略
├── main.py                   # 弃用的 Tkinter 试验品（仅作历史参考）
├── docs/
│   ├── 功能需求.md            # 产品定位、8 大模块、安全约束
│   ├── 目录清单.md            # 20+ IDE/CLI 的可迁与不可动目录
│   ├── decisions/            # 决策记录，只增不改
│   ├── tasks/TASK-XXX/       # 按任务隔离的产出
│   ├── dev/                  # 协作规范
│   ├── specs/                # 功能/架构规格最新版（规划中）
│   └── multica-handover/     # 接手报告，只读（规划中）
├── reference/                # 竞品与事故调研（只读）
└── ui/
    ├── index.html            # 静态原型，可直接用浏览器打开
    └── _shots/               # 本地截图，已忽略
```

后续阶段将按任务拆解新增 `src/`（`scan`、`rules`、`migrate`、`rollback`、`store`）、`data/`、`tests/`、`tools/`、`website/`，目录归属见 `docs/dev/协作规范.md`。

## 本地运行

当前没有任何可运行的核心功能；以下仅用于查看现有资产。

环境要求：Windows 10/11，Python 3.12（仅 `main.py` 需要，使用标准库 tkinter）。

```bash
# 查看静态原型：直接用浏览器打开 ui/index.html
# 或起一个本地静态服务（可选）：
python -m http.server 8000
# 然后访问 http://localhost:8000/ui/

# 运行已弃用的 Tkinter 试验品：
python main.py
```

`ui/index.html` 是自包含的单文件原型，不依赖网络与构建工具；它展示的是写死的数据，不会扫描或迁移任何目录。

## 文档索引

新接手请按顺序阅读：

1. `docs/tasks/TASK-001/项目现状总结.md` — 资产盘点、完成度、风险清单
2. `docs/decisions/0001-第一期范围与优先级.md` — P0 只做「扫描 → 判定 → 迁移 → 校验 → 回滚」主链路
3. `docs/功能需求.md` — 产品需求
4. `docs/目录清单.md` — 规则引擎的数据基础
5. `docs/dev/协作规范.md` — 目录归属、分支与提交规则

## 安全提示

根目录曾残留 `check*.py`、`upload*.py`、`test_ssh.py`、`clean.py` 等临时脚本，内含服务器 root 明文凭据。这些文件已被 `.gitignore` 排除，**不得提交入库**；凭据泄露风险记录在 `docs/tasks/TASK-001/项目现状总结.md` 的风险 R2，需由人类处置（轮换密码、改用 SSH key），开发人员不要自行修改服务器。完整「禁止提交清单」见 `docs/dev/协作规范.md`。

## 协作

- 仓库目前仅存在于本地，未配置远端，不得擅自推送
- 分支命名、提交信息、目录归属、文档规则见 `docs/dev/协作规范.md`
- 所有变更以 Multica issue 为入口，完成后在 issue 评论中说明产出与风险
