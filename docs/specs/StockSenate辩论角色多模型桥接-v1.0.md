# StockSenate 辩论角色多模型桥接 v1.0（QUOT-518）

> 变更记录
> - v1.0 (2026-11-10) 初稿：GET/PUT 契约、写回策略、错误码、前端契约。

## 1. 目标

让普通用户在 Disk Warden 面板里为 StockSenate 的 16 个辩论角色分别选择
provider + model，后端写进 StockSenate 的 `config/llm_config.toml`
`[llm.role_llms.<role>]`，消除「一个模型自己辩自己」的退化。

后端只负责**读写与校验**；页面由前端架构师通过子单实现。

## 2. 边界

| 事项 | 负责方 |
| --- | --- |
| 读/写 `llm_config.toml`、复用 StockSenate 校验 | 本单（后端） |
| 16 角色 provider+model 选择器、生效来源徽标、测试/保存 | 前端子单 |
| 密钥存取 | 现有 `src/core/ai/keystore.py`（DPAPI）+ 环境变量；**不进 TOML** |

## 3. 配置位置

按以下优先级解析（`src/core/ai/senate.py:_config_path`）：

1. `AppService(senate_config_path=...)` —— 仅测试注入；
2. 环境变量 `STOCKSENATE_CONFIG` —— 直接指向 `llm_config.toml`；
3. 环境变量 `STOCKSENATE_ROOT` / `STOCKSENATE_HOME` —— StockSenate 仓库根目录，
   拼 `config/llm_config.toml`。

StockSenate 自身规则**不复制、不重写**：`roles/llm.py`、`roles/schema.py`
在每次调用时从检出目录加载（`senate.load_rules`），角色常量、别名表、
provider 列表、`resolve_role_key`、`validate_config` 全部以 StockSenate
源码为准。StockSenate 升级后 Disk Warden 无需改动。

## 4. 桥接接口

沿用现有 JSON-RPC 桥（`src/app/bridge.py` 白名单），不新增 HTTP 服务。

### 4.1 `senate.role_models.get`

请求：`{}`

响应 `result`：

```json
{
  "engine": {
    "name": "StockSenate",
    "label": "StockSenate 辩论引擎",
    "config_path": "D:\\\\workplace\\\\StockSenate\\\\config\\\\llm_config.toml",
    "roles_dir": "D:\\\\workplace\\\\StockSenate\\\\roles",
    "exists": true,
    "revision": "1762758000123456789-2048",
    "authority": "toml",
    "reload": "webui_per_job"
  },
  "roles": [
    {"role": "market", "provider": "openai", "model": "gpt-5", "api_key_env": "OPENAI_API_KEY"}
  ],
  "unconfigured_roles": ["lockup"],
  "role_count": 16,
  "global": {"backend": "openai_compatible", "backend_url": "", "max_tokens": 2048},
  "providers": [
    {"name": "openai", "key_env_suggested": "OPENAI_API_KEY", "models": ["gpt-5", "..."]}
  ],
  "aliases": {"technical": "market", "basic": "fundamentals", "sentiment": "social", "unlock": "lockup"}
}
```

- `engine.revision`：文件 `mtime_ns-size`，PUT 时回传做乐观并发控制；
- `engine.authority`：`toml` 或 `env`（StockSenate 环境变量覆盖角色时，
  面板应提示「环境变量优先，TOML 修改暂不生效」）；
- `engine.reload`：`webui_per_job` —— 写后 WebUI 下一个 job 前自动重载；
  CLI/MCP 每次调用重读；**queue 常驻进程一次 drain 复用 graph，需重启
  queue 才生效**，前端保存成功后应就地提示；
- `global` 不返回任何可能像密钥的键值（`*_key`/`token`/`secret`...，`*_env` 除外）；
- `providers[].models` 是**桥接侧编写**的候选清单（StockSenate 无 model
  catalog，model 仍是自由字符串），前端下拉可编辑。

### 4.2 `senate.role_models.put`

请求：

```json
{
  "roles": {
    "market": {"provider": "anthropic", "model": "claude-opus-4-1", "api_key_env": "ANTHROPIC_API_KEY"}
  },
  "expected_revision": "<上次 GET 的 engine.revision>"
}
```

响应 `result`：

```json
{
  "written": {"market": {"provider": "anthropic", "model": "claude-opus-4-1", "api_key_env": "ANTHROPIC_API_KEY"}},
  "noop": false,
  "backup_path": "<config>.toml.bak",
  "warnings": ["alias:technical->market"],
  "engine": {"...": "与 GET 相同"}
}
```

写回流程：

1. `normalise_roles` —— 角色键经 `resolve_role_key` 归一（别名转 canonical
   并回告 warning）；`provider`/`model` 必填，长度 ≤200，禁控制字符；
   `api_key_env` 必须是合法环境变量名；
2. 结构化编辑 TOML（保留注释、空行、顺序、行内注释列对齐），新增角色
   表追加在文件末尾注释块之前；
3. 用 `tomllib` 解析候选文本 → 拒绝任何疑似密钥键/URL 内嵌凭据（写前扫描
   整个文档）→ `schema.validate_config` 校验；
4. 值与现状一致 → `noop: true`，**不写文件**；
5. 写前把旧文件复制为 `.bak`；`atomic_write_bytes` 原子替换；
6. **重新从磁盘读回**再用 `validate_config` 校验一次（以落盘内容为准）；
7. 追加审计行到 `senate-role-models.log`（时间、路径、改动角色、revision），
   不记录任何值。

## 5. 错误码

| code | retryable | 含义 |
| --- | --- | --- |
| `SENATE_NOT_CONFIGURED` | 否 | 未配置 `STOCKSENATE_ROOT`，或目录不是 StockSenate 检出 |
| `SENATE_INVALID_ROLE` | 否 | 未知角色 / 别名与 canonical 冲突 / 重复角色 |
| `SENATE_INVALID_VALUE` | 否 | provider/model 缺失、非法字符、未知字段 |
| `SENATE_SECRET_REJECTED` | 否 | 试图把字面密钥或带凭据的 URL 写进 TOML |
| `SENATE_VALIDATION` | 否 | 未通过 StockSenate `validate_config`（文件损坏/未知角色表等） |
| `SENATE_STALE` | 是 | `expected_revision` 不匹配（并发修改） |
| `SENATE_WRITE_FAILED` | 是 | 磁盘写入或备份失败 |

所有失败都在写盘前返回；文件保持原样。

## 6. 红线落实

- **密钥不进 TOML**：`api_key_env` 只存变量名；字面 `api_key/token/secret/
  password/credential` 键及 `https://user:pass@host` 直接拒绝；
- **不改 StockSenate 校验语义**：直接调用其 `validate_config`；
- **后端不写页面**：前端子单负责；
- **不新增依赖**：使用标准库 `tomllib`（3.12 运行时）+ 自研结构编辑器，
  不引入 `tomlkit`。

## 7. 已知限制

- 本单实现时 StockSenate 源码**不在任何仓库/本地检出中**（见 QUOT-518
  评论），`roles/llm.py`、`roles/schema.py` 的实际符号名按 issue 描述推断；
  `load_rules` 对拿不到的符号有兜底（角色名回落到 16 个、provider 回落到
  内置 9 个），并提供了 `STOCKSENATE_ROOT` 下的真实检出测试
  （`tests/test_senate.py::RealCheckoutTests`，未设置时跳过）。拿到真实检出后
  需跑一次该测试确认符号对齐。
- TOML 编辑器覆盖常见写法（表头、键值、单行字符串/数组、行内注释、
  多行字符串跳读）；复杂内联表/数组跨行不改写，但仍参与校验。
