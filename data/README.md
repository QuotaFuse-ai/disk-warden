# data/ 规则数据说明

本目录把 `docs/目录清单.md` 转成程序可直接消费的结构化数据。当前范围只包含数据与校验脚本，不包含扫描、迁移逻辑。

| 文件 | 作用 |
|---|---|
| `ide-catalog.json` | 已收录 IDE/CLI/开发工具的目录清单，每个条目一条 |
| `rules.json` | 按文件/目录名匹配的安全分类规则，用于覆盖清单外的新目录 |
| `validate_rules.py` | 位于 `tools/`，校验两份 JSON 的 schema、路径、重复项与安全等级 |

当前规模：30 个软件条目、162 条目录、131 绿 / 28 红 / 3 黄、27 条名称规则。`docs/目录清单.md` 中的条目已全部结构化，包括 Trae CN 与 TRAE SOLO CN 的新旧两套根目录。

---

## 一、ide-catalog.json

### 1. 顶层字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema_version` | 整数 | 当前为 `1`，结构变更时递增 |
| `source` | 字符串 | 数据来源文档 |
| `platform` | 枚举 | `windows` / `macos` / `linux`；当前数据全部为 Windows |
| `updated_at` | 字符串 | 最后更新日期，`YYYY-MM-DD` |
| `ides` | 数组 | 软件条目列表 |

### 2. 软件条目（`ides[]`）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | 字符串 | 是 | 稳定英文标识，小写字母/数字/连字符，全局唯一，例如 `trae-cn`。程序内部引用它，不要随意改 |
| `name` | 字符串 | 是 | 显示名，可含中文 |
| `kind` | 枚举 | 是 | `ide` / `cli` / `extension` / `tool` |
| `platform` | 枚举 | 是 | 同顶层 |
| `processes` | 字符串数组 | 是 | 进程名，用于迁移前占用检测；插件或纯缓存工具可为空数组。Windows 进程名不区分大小写，不要只靠大小写区分 |
| `notes` | 字符串 | 否 | 改名历史、共享目录、特殊迁移方式等人工说明 |
| `directories` | 数组 | 是 | 该软件的目录条目，不能为空 |

### 3. 目录条目（`ides[].directories[]`）

每条必须包含以下四个字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `path` | 字符串 | Windows 路径，必须使用占位符，见下文 |
| `type` | 枚举 | `dir`（目录）/ `file`（文件）/ `glob`（通配文件） |
| `safety` | 枚举 | `green` / `red` / `yellow` |
| `reason` | 字符串 | 给人看的中文原因，包含体积量级或安全说明 |

可选字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `handling` | 枚举 | `junction`（默认）/ `env-var` / `app-config` / `docker-settings`。表示该路径推荐的迁移方式，不影响安全等级 |

### 4. 路径写法

- 只允许三种前缀：`%APPDATA%`、`%USERPROFILE%`、`%LOCALAPPDATA%`。
- 禁止写死盘符（如 `C:\`）和用户名（如 `\Users\Administrator\`）。
- 目录必须以反斜杠结尾，文件不能以反斜杠结尾。
- 未知的单层名称用尖括号占位：`<项目>`、`<session>`、`<仓库名>`；多个占位符可连写，如 JetBrains 的 `<产品><版本>`。
- 只有 `type` 为 `glob` 时允许 `*`，且只能出现在最后一段，例如 `workspaceStorage\*.pack`；禁止 `*` 和 `**` 这类过宽匹配。
- 同一路径（忽略大小写）在整份清单中只能出现一次。

### 5. safety 的含义

| 值 | 行为约定 |
|---|---|
| `green` | 可再生或明确可迁；允许自动迁移 |
| `red` | 凭证、配置、记忆、密钥；任何情况下不自动迁移，也不随父目录整体搬移 |
| `yellow` | 清单/规则无法确认；必须由用户显式确认后才能操作 |

注意父子路径可能安全等级不同。例如 Claude Code 的 `projects\<项目>\` 是绿色，但其下的 `memory\` 是红色。扫描与迁移实现必须按最长路径优先匹配：父目录整体迁移前先剔除红色子路径，或只迁移明确列出的子目录。

---

## 二、rules.json

用于规则引擎（第二层识别）：拿到一个目录名或文件名时，按名称判断安全等级。

### 1. 顶层字段

| 字段 | 值 | 说明 |
|---|---|---|
| `schema_version` | `1` | 结构版本 |
| `name_scope` | `basename` | 只匹配路径最后一段的名称，不匹配完整路径 |
| `case_sensitive` | `false` | Windows 名称不区分大小写 |
| `conflict_resolution` | `red_wins` | 多条规则同时命中时红色优先 |
| `default` | 对象 | 未命中任何规则时的兜底，固定为 `yellow` |
| `rules` | 数组 | 规则列表 |

### 2. 规则条目（`rules[]`）

| 字段 | 类型 | 说明 |
|---|---|---|
| `match` | 字符串 | 文件/目录名，不能包含路径分隔符、占位符或首尾空格 |
| `match_type` | 枚举 | `exact` / `glob` / `regex` |
| `safety` | 枚举 | `green` / `red` / `yellow` |
| `reason` | 字符串 | 中文原因 |

匹配语义：

1. 先计算所有命中的规则；
2. 任一红色规则命中即判红，红色优先于绿色；
3. 否则任一绿色规则命中即判绿；
4. 都不命中时使用 `default`，判黄。

当前规则覆盖 Cache、CachedData、GPUCache、Code Cache、logs、debug、`*.log`、`*.sqlite`、sessions、projects、tool-results、checkpoints、workspaceStorage、globalStorage、image-cache、paste-cache、uploads，以及 Login Data、Local State、settings.json、settings.local.json、`.credentials.json`、`.credentials.yaml`、auth.json、`.env`、memory、memories。

---

## 三、新增一个 IDE 的步骤

1. 在 `docs/目录清单.md` 补上调研结果，标明路径、体积量级、可迁结论和原因。
2. 在 `data/ide-catalog.json` 的 `ides` 数组末尾新增一个对象：
   - `id` 取小写英文稳定标识，确认不与现有 id 重复；
   - `kind` 按软件类型选择 `ide` / `cli` / `extension` / `tool`；
   - `processes` 填主进程名；Windows 下大小写不敏感；
   - 每条目录写全 `path` / `type` / `safety` / `reason`，路径使用占位符且目录以 `\` 结尾。
3. 如果出现了新的名称模式（例如新的缓存目录名），在 `data/rules.json` 的 `rules` 里补一条规则；只对单个软件有效的路径不要写成通用名称规则，避免误伤其他软件。
4. 凭证、配置、记忆类路径一律标 `red`；无法确认的标 `yellow`，不要凭感觉标绿。
5. 运行校验：

```bash
python tools/validate_rules.py
```

期望输出类似：

```text
IDEs:         30
Directories:  162
Safety:       green=131 red=28 yellow=3
Rules:        27
OK: validation passed
```

有错误时脚本打印带位置的错误列表并返回退出码 1，例如重复路径、未知占位符、写死用户名、safety 枚举错误、红色名称被标绿等。

也可以指定其他文件路径：

```bash
python tools/validate_rules.py --catalog data/ide-catalog.json --rules data/rules.json
```

---

## 四、维护约定

- 改路径或改 `safety` 必须同步更新 `reason`，并运行校验脚本。
- 红色条目只增不减；确需降级必须有实测依据并记录在 `docs/specs/`。
- 产品改名（如 Trae CN → TRAE SOLO CN、Windsurf → Devin）时保留旧目录条目，新增新目录条目，不要直接替换，避免旧数据被漏扫。
- 共享同一数据目录的软件合并到一个条目并在 `notes` 说明，避免同一路径重复出现（例如 Codex CLI 与桌面版共用 `%USERPROFILE%\.codex\`，Windsurf 与 Devin 共用 `.codeium` / `.windsurf`）。
- 本目录数据只描述"哪些路径、能不能动"，迁移顺序、备份、校验、回滚由后续的迁移模块负责。
