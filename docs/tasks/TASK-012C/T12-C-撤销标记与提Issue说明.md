# T12-C 撤销标记与提 Issue 说明

- 任务：QUOT-28 / T12-C「撤销标记」回自动学习态 + 「提 Issue」拉起系统浏览器
- 日期：2026-10-20
- 负责人：主机端开发
- 状态：已实现，待验收
- 分支：`feat/t12c-undo-override-open-issue`（基于 PR #1 head 分支 `agent/agent/84e365cc1ed1`）

## 1. 范围与结论

本期只做两件事，不触碰迁移/回滚安全逻辑，不修改既有 `docs/specs/` 版本号：

1. 已学习规则里的人工标记可以撤销，撤销后该目录回到规则引擎的自动判定（黄色重新进入待确认列表）。
2. 反馈弹窗的「提 Issue」调用主机端打开系统默认浏览器，落到仓库的新 Issue 页面；诊断信息仍只走「复制诊断」。

## 2. RPC 契约

### 2.1 `rules.override.clear`

| 项目 | 内容 |
| --- | --- |
| 参数 | `{ "item_id": string }` |
| 成功 | `{ "item_id": string, "cleared": true }` |
| 不存在 | `NOT_FOUND`（记录不存在，或该规则不是 `rule_id="manual"`） |
| 参数非法 | `INVALID_ARGUMENT` |
| 副作用 | 删除 `rules-learned.json` 中该条人工规则，重建规则引擎并刷新快照的 `safety/source/rule_id/reason/requires_confirm/can_migrate/stats` |

语义锁定：

- 只影响传入的 `item_id`，其他人工规则保持不变；
- 撤销后该目录从「已学习规则」消失，并按真实自动判定回到待确认列表（黄）或直接可迁（绿）；
- `SAFETY_RED` 终态约束不变：自动判红仍不可被绿色覆盖；人工标记（包括人工红）始终可撤销，撤销后回到自动判红。

### 2.2 `app.open_external`

| 项目 | 内容 |
| --- | --- |
| 参数 | `{ "url": string }` |
| 成功 | `{ "opened": true, "url": string }` |
| 白名单外 | `FORBIDDEN` |
| 参数非法 | `INVALID_ARGUMENT`（空串、非字符串、超过 2048 字符、含控制字符） |
| 浏览器启动失败 | `IO_ERROR`（不会抛未捕获异常） |

白名单规则（`src/app/service.py:_validate_external_url`）：

- scheme 必须是 `https`；
- host 必须是 `github.com`（允许省略端口或 `:443`，禁止 userinfo 与其他端口）；
- path 必须以 `/QuotaFuse-ai/` 开头；
- 拒绝反斜杠、`.` / `..` 路径段（含百分号编码形式），避免浏览器归一化后跳出组织目录。

`http:`、`file:`、`javascript:`、其他域名、`github.com/其他组织/...` 一律 `FORBIDDEN`，且在调用系统浏览器前就被拒绝。

## 3. UI 行为

- 规则列表新增「操作」列：仅 `rule_id === "manual"` 的行显示「撤销标记」；按钮为中性 ghost 样式，不是危险红，无二次确认。
- 点击撤销成功：该行消失并 toast「已恢复自动判定」；待确认列表按新的自动判定同步刷新。
- 撤销失败：toast「撤销失败：{原因}」；`NOT_FOUND` 显示「该标记已不存在，请刷新后重试」。
- 反馈弹窗「提 Issue」：调用 `app.open_external` 打开 `https://github.com/QuotaFuse-ai/disk-warden/issues/new`；成功 toast「已在浏览器中打开 Issue 页面」，失败 toast「无法打开浏览器：{原因}」。
- 诊断信息不拼进 URL，仍只能通过「复制诊断」带走。

## 4. 测试

新增单测：

- `tests/test_app_rules_override.py`：撤销成功回到自动判定、只影响目标条目、记录不存在 / 非人工规则返回 `NOT_FOUND`、空参数、自动红上的人工标记可撤销；
- `tests/test_app_external.py`：白名单命中、`http/file/javascript`/其他域名/其他组织/路径穿越/端口/userinfo 拒绝、参数非法、浏览器失败映射 `IO_ERROR`；
- `tests/test_platform_external.py`：`webbrowser.open` 参数、返回 False 与异常包装；
- `tests/test_app_bridge.py`：两个新方法进入白名单、参数校验与转发。

全仓 `python -m unittest discover -s tests -t . -q`：585 个测试通过。

## 5. 手工验收（打包产物）

1. 用临时 `DISKWARDEN_DATA_ROOT` 启动打包后的 `DiskWarden.exe`，进入规则页人工标记一个黄色目录，再点该行的「撤销标记」：行消失、toast 出现、该目录回到待确认列表。
2. 打开「反馈问题」，点「提 Issue」：系统默认浏览器打开 `https://github.com/QuotaFuse-ai/disk-warden/issues/new`；「复制诊断」行为不变。
3. 检查临时数据目录下的 `rules-learned.json`：撤销的条目已删除，其他条目不受影响；真实用户数据目录未被迁移或改写。
