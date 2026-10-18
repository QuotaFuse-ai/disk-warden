# T22 AI 分析接口与联网适配器说明

- 任务：QUOT-21 / T22「AI 分析接口与联网适配器」
- 日期：2026-10-19
- 负责人：后端架构师
- 状态：已实现，待验收
- 上游依据：`docs/decisions/0006-启用联网AI分析未知目录.md`、`docs/decisions/0005-Python版本基线与macOS首次启动引导.md`、`src/core/rules/models.py` 中的 `AIResolver` 协议

## 1. 范围与结论

本期交付第三层 AI 分析的**全部接口与联网适配器**，只作用于规则引擎判为黄色（未知）的条目：

- `src/core/ai/`：配置、隐私脱敏、OpenAI 兼容 HTTP 适配器、提示词与响应解析、结果缓存、`AIResolver` 实现；
- 通过既有 `ClassifyEngine(ai_resolver=...)` 钩子注入，**未修改 `src/core/rules/` 的任何既有规则逻辑**；
- CLI 新增显式开关 `--ai`，默认关闭；无 API key 时打印一条提示后按原逻辑继续；
- 单测全部使用假传输，不访问真实网络；真实 endpoint / model / key 待人类提供后再做联网验收。

结论：

- 新增 122 个单测；全仓 **232 个测试全部通过**（原基线 110 个 + 新增 122 个）。
- AI 结论一律 `requires_confirm=True`、`can_migrate=False`，且永远无法覆盖目录表/模式规则的红色终态。
- 发送前做“脱敏 + 序列化 + 泄漏复查”三重检查；一旦发现用户名、家目录、盘符或环境变量值残留，直接拒绝本次调用，条目保持黄色。
- 超时、断网、HTTP 错误、限流、非法 JSON、非法结论全部降级为黄色，不阻塞扫描。
- 默认单次扫描最多 20 次调用、60000 估算 token；缓存命中与预算超限均有可观测日志。

## 2. 产出文件

| 文件 | 作用 |
|---|---|
| `src/core/ai/__init__.py` | 公开 API 汇总导出 |
| `src/core/ai/config.py` | `AIConfig`、`load_config`、默认值与可用性判定 |
| `src/core/ai/payload.py` | 元数据组装、路径脱敏、泄漏检测 |
| `src/core/ai/provider.py` | `AIProvider` 协议、`OpenAICompatibleProvider`、`ProviderError` |
| `src/core/ai/resolver.py` | `AIResolver`、提示词、请求构建、响应解析、预算与缓存编排 |
| `src/core/ai/cache.py` | `AICache`、缓存键、原子写、TTL 与条目上限 |
| `src/core/scan/__main__.py` | CLI `--ai` / `--rules` / `--ai-cache` 与 JSON/人类可读输出 |
| `tests/test_ai_payload.py` | 23 个脱敏与序列化测试 |
| `tests/test_ai_provider.py` | 19 个 HTTP 适配器测试（假 opener） |
| `tests/test_ai_config.py` | 17 个配置与开关测试 |
| `tests/test_ai_resolver.py` | 41 个解析、降级、预算、缓存及引擎集成测试 |
| `tests/test_ai_cache.py` | 12 个缓存键、TTL、原子写、健壮性测试 |
| `tests/test_ai_cli.py` | 10 个 CLI 开关、提示、JSON 输出与隐私测试 |
| `docs/tasks/TASK-022/AI分析接口说明.md` | 本文档 |

## 3. 配置

配置优先级：**环境变量 > 设置项（settings 映射） > 默认值**。显式传入的 `enabled`（CLI `--ai`）优先于环境变量与设置开关。

| 环境变量 | settings 键 | 默认值 | 说明 |
|---|---|---|---|
| `DISK_WARDEN_AI_ENABLED` | `enabled` | 关闭 | `1/true/yes/on` 开启；CLI `--ai` 可显式覆盖 |
| `DISK_WARDEN_AI_BASE_URL` | `base_url` | 空 | OpenAI 兼容 endpoint，如 `https://host/v1`；自动补 `/chat/completions` |
| `DISK_WARDEN_AI_MODEL` | `model` | 空 | 模型名 |
| `DISK_WARDEN_AI_API_KEY` | `api_key` | 空 | **必需**；缺失时功能不可用 |
| `DISK_WARDEN_AI_MAX_CALLS` | `max_calls` | `20` | 单个 resolver 的网络调用上限，范围 0–10000 |
| `DISK_WARDEN_AI_MAX_TOKENS` | `max_total_tokens` | `60000` | 估算 token 上限，`0` 表示不限制 |
| `DISK_WARDEN_AI_MAX_OUTPUT_TOKENS` | `max_output_tokens` | `200` | 单次请求输出上限，范围 1–4096 |
| `DISK_WARDEN_AI_TIMEOUT` | `timeout_seconds` | `10` | 单次请求超时（秒），适配器内最多 120 |
| `DISK_WARDEN_AI_CACHE` | `cache_path` | 用户缓存目录 | 默认 Windows `%LOCALAPPDATA%\disk-warden\ai-cache.json`，macOS/Linux `$XDG_CACHE_HOME` 或 `~/.cache` |
| `DISK_WARDEN_AI_CACHE_TTL` | `ttl_seconds` | `2592000`（30 天） | 缓存有效期 |
| `DISK_WARDEN_AI_MAX_PATH_SEGMENTS` | — | `3` | 每条目发送的路径片段数，硬上限 6 |

可用性判定（`AIConfig.disabled_reason`）：

| 原因码 | 含义 |
|---|---|
| `disabled` | 开关未开启（默认状态） |
| `missing_api_key` | 未配置 key |
| `missing_base_url` | 未配置 endpoint |
| `missing_model` | 未配置模型 |

只有 `disabled_reason` 为空时才会创建 provider 并发起调用。key 只从环境变量/设置读取，**不写入仓库**，也不出现在 `repr()`、`public_dict()`、`stats()` 或任何日志行中。

## 4. 调用边界

调用链：

```
discover → measure → ClassifyEngine.classify(item)
                         ├─ 目录表/学习记录/名称规则命中 → 直接返回（红色为终态）
                         └─ 仅当结论为黄色且注入了 resolver
                                └─ AIResolver.classify(item, current)
```

硬边界：

1. **只处理黄色**：`AIResolver.classify` 首先检查 `current.safety == yellow`；绿色/红色直接返回 `None`，不会构造请求。
2. **红色终态不可覆盖**：引擎在调用 AI 之前已对红色直接返回；AI 即便返回 `red`/`green`，也由引擎重建为 `source="ai"`、`rule_id="ai"`、`requires_confirm=True`。
3. **不参与自动迁移**：AI 结论的 `can_migrate` 恒为 `False`，必须用户逐项确认。
4. **不重试**：适配器对任何失败只返回一次错误，避免放大费用并使调用预算不可预测。
5. **不改规则逻辑**：本次只使用既有 `ClassifyEngine(ai_resolver=...)` 注入点；`src/core/rules/` 无逻辑改动。

### 请求形态

OpenAI 兼容 `POST {base_url}/chat/completions`，`temperature=0`，`max_tokens=max_output_tokens`。system 提示词要求模型只回答一个 JSON 对象：

```json
{"safety": "green|red|yellow", "reason": "简短理由"}
```

- `green`：明确的缓存、日志、临时文件、构建产物等可再生数据；
- `red`：凭据、许可证、配置、用户文档、项目源码、数据库等；
- `yellow`：证据不足或没有把握（等价于“不给出建议”）。

响应解析容忍 Markdown 代码围栏和 JSON 前后多余文本；`yellow`、未知 safety、缺失/空理由、非法 JSON 一律视为“无建议”，条目保持黄色。

## 5. 隐私边界

### 5.1 发送字段

```json
{
  "schema_version": 1,
  "path": "Roaming/Code/workspaceStorage",
  "path_segments": ["Roaming", "Code", "workspaceStorage"],
  "depth": 4,
  "file_count": 1234,
  "total_bytes": 5678901,
  "extensions": {".log": 120, ".json": 30, "__other__": 5}
}
```

只包含相对路径片段、层级、文件数、总字节数、扩展名分布。**不发送文件内容、文件名、绝对路径、用户名、环境变量值。**

### 5.2 脱敏流程

1. 优先使用扫描层给出的 `rel_path`；否则从绝对路径剥离最长匹配的家目录/环境变量前缀，再去掉盘符；
2. UNC 路径（`\\host\share\...`）丢弃 host 与 share；
3. 每个片段：用户名替换为 `<user>`，任何环境变量值替换为 `<env>`，去除控制字符，截断到 48 字符；
4. 只保留最后 N 个片段（默认 3，上限 6），层级上限 32；
5. 扩展名归一化为小写点号形式，最多 12 个，其余聚合为 `__other__`。

### 5.3 最后防线

序列化后再次执行 `leak_categories()`，检查 `username`、`home_directory`、`environment_value`、`drive_letter` 四类残留。只要命中任一类别：

- **不发起网络请求**；
- 日志只记录类别名（不记录具体值），例如 `ai payload refused (categories: drive_letter,username)`；
- 条目保持黄色。

### 5.4 日志与密钥

- provider 从不记录请求头或 body，`__repr__` 不包含 key；
- `AIResolver.stats()` 只输出计数、预算和原因码；
- CLI 的 AI 日志走 stderr（logger `disk_warden.ai`），不污染 `--json` 的 stdout；
- 测试中校验 stdout/stderr 均不含 key、用户名与家目录。

## 6. 成本控制

| 机制 | 行为 |
|---|---|
| 调用次数上限 | 每个 resolver 默认 20 次；达到上限后后续黄色条目直接跳过，记 `budget_skips` 并输出 warning |
| token 上限 | 请求预估 token = 提示词字符数/4 + 输出上限；默认 60000；实际 `usage.total_tokens` 优先；超限跳过 |
| 输出上限 | 单次 `max_output_tokens=200`，限制回答长度 |
| 超时 | 默认 10 秒，避免长时间挂起 |
| 结果缓存 | 命中不发请求；键 = `sha256(脱敏路径 + 规则版本 + 提示词版本)[:32]` |
| 规则版本 | `sha256(data/rules.json 内容)[:16]`；规则文件变化自动失效 |
| 提示词版本 | 常量 `PROMPT_VERSION = "1"`；提示词变化时递增 |
| 缓存 TTL | 默认 30 天；条目上限 5000，按创建时间淘汰 |
| 缓存持久化 | 同目录临时文件 + fsync + `os.replace` 原子替换；损坏文件按空缓存处理，写失败只记日志 |

可观测日志（全部在 `disk_warden.ai` logger）：

- `ai cache hit path=... rules=... prompt=...`
- `ai call budget exhausted (n/max); keeping item yellow`
- `ai token budget exhausted (n/max); keeping item yellow`
- `ai suggestion path=... safety=... tokens=...`
- `ai request failed code=... status=...; keeping item yellow`
- `ai payload refused (categories: ...); keeping item yellow`

## 7. 失败降级

以下情况统一返回 `None`，条目保持黄色，扫描继续：

| 情况 | 错误码 / 处理 |
|---|---|
| 超时（连接/读取） | `timeout` |
| 断网、DNS、连接拒绝 | `network` |
| 非 429 的 HTTP 错误 | `http`（含状态码） |
| 429 限流 | `rate_limited` |
| 响应非 JSON / 非对象 / 无 choices / 超过 1 MB | `invalid_response` |
| 模型回答 `yellow`、未知 safety、缺理由 | 视为无建议 |
| 配置缺失或开关关闭 | `available=False`，不构造请求 |
| 脱敏复查失败 | 拒绝请求 |
| 调用/token 超限 | 跳过请求 |
| 缓存读写异常 | 忽略缓存，继续正常流程 |

CLI 层额外保证：`--ai` 未同时指定 `--sizes`、规则文件不可用时，都只打印一条提示并按原逻辑输出，退出码仍为 0。

## 8. CLI 用法

```bash
# 默认关闭：输出与未加 --ai 时完全一致
python -m src.core.scan --json

# 显式开启（必须配合 --sizes 统计元数据）
python -m src.core.scan --sizes --ai --json \
  --rules data/rules.json \
  --ai-cache /path/to/ai-cache.json
```

所需环境变量：

```bash
DISK_WARDEN_AI_BASE_URL=https://your-provider/v1
DISK_WARDEN_AI_MODEL=your-model
DISK_WARDEN_AI_API_KEY=...        # 仅注入环境，不写入仓库
DISK_WARDEN_AI_MAX_CALLS=20
```

行为：

- **未配置 key**：stderr 打印一条 `AI 分析未启用：未配置 DISK_WARDEN_AI_API_KEY（或缺少 endpoint/model），已按原逻辑继续。`，扫描结果与未开启时一致；
- **`--ai` 未带 `--sizes`**：stderr 打印一条 `AI 分析未启用：--ai 需要同时指定 --sizes。`；
- `--json` 输出新增 `ai` 段（仅在成功构建 resolver 时出现）：

```json
{
  "ai": {
    "stats": {
      "enabled": true, "available": true, "disabled_reason": "",
      "calls": 1, "cache_hits": 0, "suggestions": 1, "failures": 0,
      "refused_payloads": 0, "budget_skips": 0, "estimated_tokens": 123,
      "max_calls": 20, "max_total_tokens": 60000
    },
    "summary": {"green": 1, "yellow": 0, "red": 0},
    "suggestions": [
      {
        "item_id": "...", "tool_id": "adhoc-tool",
        "rel_path": "AppData/Local/MysteryTempDir",
        "safety": "green", "source": "ai",
        "reason": "regenerable cache data",
        "requires_confirm": true, "can_migrate": false
      }
    ]
  }
}
```

- 人类可读输出在 `--sizes` 之后追加一行统计，并逐条列出 AI 建议，带 `(requires confirmation)` 后缀。

## 9. 测试

运行方式（仓库根目录，Python 3.8.6 兼容）：

```bash
python -m unittest discover -s tests -v
```

结果：**232 个测试全部通过**，其中新增 122 个：

| 文件 | 数量 | 覆盖点 |
|---|---|---|
| `test_ai_payload.py` | 23 | 用户名/家目录/盘符/环境变量剥离、UNC、片段上限、扩展分布、泄漏复查 |
| `test_ai_provider.py` | 19 | 假 opener、请求头/超时、429/HTTP/超时/断网/非法 JSON/超限、不重试、repr 不泄露 key |
| `test_ai_config.py` | 17 | 默认关闭、可用性原因码、开关显式覆盖、数值解析与裁剪、缓存路径 |
| `test_ai_resolver.py` | 41 | 仅黄色触发、红/绿不触发、发送内容已脱敏、泄漏拒绝、缓存命中、token/调用上限、各类降级、引擎集成与 `requires_confirm` |
| `test_ai_cache.py` | 12 | 缓存键稳定性、TTL、淘汰、原子写、损坏文件、内容版本 |
| `test_ai_cli.py` | 10 | 默认输出不变、两条提示、JSON `ai` 段、缓存命中不重复请求、红色不联网、规则失败降级、人类可读确认标记 |

关键验收测试：

- `fake transport → 黄色目录获得建议`：建议 `safety=green`、`source=ai`、`requires_confirm=True`、`can_migrate=False`；
- `offline → 保持黄色`：provider 抛 `ProviderError(network/timeout)` 后条目仍为黄色，`failures` 计数增加；
- `cache hit → 不发请求`：第二次运行 `calls=0`、`cache_hits=1`；
- `无 key → 输出不变`：JSON 中不出现 `ai` 键，stderr 只有一条提示。

## 10. 已知限制与后续

1. **尚未做真实联网验证**：当前 `ScanItem` 不包含扩展名分布，payload 中 `extensions` 为 `{}`；真实 endpoint / model / key 与费用上限需人类提供后再验收。
2. **回调式 UI 接入待 T12**：本期只提供 CLI 与 `AIResolver` 接口，主界面开关与“目录信息将发送到第三方 AI 服务”的提示由 T12/T21 落地（决策 0006 第 4、8 条）。
3. **缓存按规则文件内容哈希失效**：若规则在同一次扫描中热更新，需要新的 resolver 实例才能感知。
4. **不重试**：限流与瞬时网络错误直接降级为黄色；如需重试，应由上层在下一轮扫描中自然重试（缓存会命中已成功条目）。

## 11. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-10-19 | 首版：完成接口、适配器、脱敏、缓存、预算、CLI 与测试 |
