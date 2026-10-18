# T10 迁移引擎 B 说明：换位、建链、读回验证与自动恢复

- 任务：QUOT-12 / T10
- 日期：2026-10-19
- 负责人：主机端开发
- 状态：已实现，待验收
- 上游依据：`docs/specs/架构设计-v1.0.md` §6.1/§6.2/§6.3/§6.4/§6.5/§6.8/§6.9、`docs/decisions/0004-第一期范围修订-无官网与双平台客户端.md`、`docs/decisions/0005-Python版本基线与macOS首次启动引导.md`、T09 交付物 `docs/tasks/TASK-009/迁移引擎A说明.md`

## 1. 范围与结论

本任务实现迁移的后半段 **S5–S9**，输入是 T09 `prepare_copy()` 产出的、已通过校验的 `MigrationResult`：

1. **S5 换位**：把暂存目录同卷原子改名为最终目标；
2. **S6 源换位**：把源目录同父目录改名为 `.dwtombstone-<job8>`，**从不递归删除源**；
3. **S7 建链**：在源路径上用 `mklink /J` 建 junction（非 Windows 用目录符号链接）；
4. **S8 读回验证**：读回链接目标并规范化比较，再经链接探测读取一个真实文件；
5. **S9 清理**：只有链接验证通过后才删除 tombstone。

结论：

- 只有校验通过才换位；只有链接读回验证通过才删除 tombstone；任何失败都不会让源数据凭空消失。
- 源路径被原子改名而不是删除；tombstone 在 S8 通过前一直保留，是失败回滚的数据来源。
- 失败自动恢复到"数据在、链接无、配置可读"：S5/S6/S7 失败均回滚；证据不充分时进入 `RECOVERY_REQUIRED` 且**不再做任何删除**。
- 所有步骤都是幂等 `ensure_*`：重跑不会重复改名、重复建链或重复删除。
- `recover_job()` 按架构 §6.4 恢复矩阵处理启动时发现的残留状态，可重复执行。
- 进度事件覆盖 `swap / link / link_verify / cleanup / recover` 五个阶段。
- Windows 上用**真实 robocopy + 真实 `mklink /J`** 完成 4 个沙箱演练（成功、建链失败回滚、建链前崩溃、建链后崩溃），见 §10。
- 新增 28 个单元测试；全套 260 个测试全部通过。

本任务**不包含** journal 持久化（状态落盘的时机与格式）、迁移历史、UI 编排与备份保留策略，这些属于 T11。

## 2. 产出文件

| 文件 | 作用 |
|---|---|
| `src/core/migrate/ops.py` | 可注入文件系统原语 `MigrationOps`（链接/改名/同卷/读字节） |
| `src/core/migrate/swap.py` | S5/S6 幂等换位、tombstone 还原、暂存/目标定向清理 |
| `src/core/migrate/link.py` | S7 建链、S8 读回验证与探测读、只删本 job 链接 |
| `src/core/migrate/complete.py` | `complete_migration()` 编排 S5–S9、失败回滚 |
| `src/core/migrate/recover.py` | `observe()` + `recover_job()`，实现 §6.4 恢复矩阵 |
| `src/core/migrate/marker.py` | 目标目录所有权标记 `.dw-owner-<job8>.json`（校验后写入） |
| `src/core/migrate/cleanup.py` | 按 job8 命名/标记删目录，reparse point 只删链接 |
| `src/core/migrate/models.py` | `CompletionResult`、阶段/状态/错误码、恢复状态集合 |
| `src/core/migrate/__init__.py` | 包入口与公开 API |
| `src/platform/link.py` | 便携链接契约；修复 `same_volume` 的平台实现解析 |
| `src/platform/win/link.py` | Windows junction 与卷身份（唯一 `mklink` 调用点） |
| `tests/migrate_fakes.py` | 共享测试夹具：内存 junction `FakeOps`、迁移目录夹具 |
| `tests/test_migrate_complete.py` | 11 个 S5–S9 编排/回滚/进度测试 |
| `tests/test_migrate_recover.py` | 17 个恢复矩阵/幂等测试 |
| `tools/migrate_drill_b.py` | 真实 robocopy + 真实 junction 的沙箱演练 |
| `docs/tasks/TASK-010/迁移引擎B说明.md` | 本文档 |

### 关于目录位置

issue 的交付物写作 `src/migrate/`，冻结架构把迁移层定位为 `core/migrate`。本任务与 T06–T09 一致落到 `src/core/migrate/`；包内只用相对导入，如确需回到 `src/migrate/`，移动整个包即可。

## 3. 分层与调用关系

```
complete.complete_migration        # S5–S9 编排与回滚
  ├─ swap.ensure_target_ready      # S5 暂存 -> 最终
  ├─ swap.ensure_source_renamed    # S6 源 -> tombstone
  ├─ link.ensure_link              # S7 mklink /J
  ├─ link.verify_link              # S8 读回 + 探测读
  ├─ cleanup.remove_tombstone      # S9
  └─ scan.process.detect_processes # 换位前进程复查
recover.recover_job                # 观察 -> 按 §6.4 分派
```

- `src/core/migrate/` 不导入 `subprocess` / `ctypes`；`mklink` 与卷信息集中在 `src/platform/win/link.py`（决策 0004）。
- 所有文件系统动作经由 `MigrationOps`，演练/测试可只替换其中一个操作（架构 §6.9）。默认真实实现委托 `src.platform`。
- macOS/Linux 的 `create_junction` 用目录符号链接；完整 macOS 适配按决策 0004 由 T21 负责。

## 4. 命名与前提

```
最终目标：<target_root>/<tool_id>/<basename>            （自带源目录名）
暂存叶子：<target_root>/<tool_id>/.dw-partial-<job8>/<basename>
tombstone：<source_parent>/.dwtombstone-<job8>
所有权标记：<final_path>/.dw-owner-<job8>.json
```

- S5 要求暂存与最终同父目录、同卷、同叶子名；暂存父目录名必须是本 job 的 `.dw-partial-<job8>`。
- S6 要求 tombstone 与源同父目录、同卷，且名字匹配本 job。
- 源、暂存、最终、tombstone 都是目录；reparse point 一律不当作普通目录处理。
- 目标已存在时，只有带**本 job 所有权标记**且 `final_path` 与记录一致才视为"上次已换位"，否则拒绝并保持源不动。
- 所有权标记在 S4 校验**之后**、S5 改名前写入暂存目录，因此不会影响 T09 的文件数/字节数比对。

## 5. S5–S9 步骤

### 5.1 S5 换位

1. 再次检查产物：`result.ok`、`verify.ok`、`job_id` 合法、源是普通目录、暂存是普通目录（或最终已存在）。
2. 进程复查：工具运行中或进程枚举失败（`unknown`）→ `PROCESS_BUSY`，清理本 job 暂存/目标，源不动。
3. 取消检查：在改名前响应取消。
4. 写入 `.dw-owner-<job8>.json`（原子写：`.tmp` + `os.replace`）。
5. `os.rename(staging/<basename>, final_path)`；同卷改名是原子的。
6. 删除已空的 `.dw-partial-<job8>` 父目录（含 `robocopy.log`），不留临时残留。

### 5.2 S6 源换位

- `os.rename(source, tombstone)`，同父目录同卷，原子。
- 源与 tombstone 同时存在 → 状态不明，抛 `SwapError`，不做猜测。
- 只改名、不删除；源数据完整躺在 tombstone 里。

### 5.3 S7 建链

- 平台层执行 `cmd /c mklink /J <source> <final>`，无需管理员权限。
- 源路径已存在且是**指向本 final 的 junction** → 幂等成功（`created=False`）。
- 源路径存在但指向别处 / 不是 junction → `LinkStepError(mismatch=True)`，**绝不删除**，作业转 `RECOVERY_REQUIRED`。
- 建链后立即读回目标；读回值不匹配即失败并触发回滚。

### 5.4 S8 读回验证

- 读回 junction 目标（`os.readlink`，去掉 `\\?\` 前缀），用 `link_target_matches` 规范化比较大小写、相对段与 **8.3 短路径**（Windows 常返回 `ADMINI~1`），不直接比较字符串。
- 在最终目录里找第一个普通文件（跳过所有权标记与 reparse point），分别经链接和直接路径各读 1 字节并比较；空目录树退化为两侧目录列举。
- 探测读失败（权限、链接不可遍历）同样判失败。

### 5.5 S9 清理

- 仅在 S8 通过后调用 `remove_tombstone`。
- 删除前校验 tombstone 名字匹配本 job 的 8 位前缀；根是 reparse point 时拒绝；子项是 reparse point 时只删链接、不进入目标。
- 删除失败 → `LINKED_CLEANUP_PENDING`（链接已可用，仅剩重复数据），由 `recover_job` 重试；**已成功的链接不会被拆掉**。

## 6. 失败与回滚矩阵

| 情况 | 结果状态 | 错误码 | 目标 | 暂存 | tombstone | 源 |
|---|---|---|---|---|---|---|
| 输入不是已校验产物 | `FAILED` | `INVALID_RESULT` | 不触碰 | 保留 | — | 不变 |
| 工具运行中 / 进程枚举失败 | `FAILED` | `PROCESS_BUSY` | 本 job 目标删除 | 删除 | — | 不变 |
| S5 前取消 | `FAILED` | `CANCELLED` | 未创建 | 删除 | — | 不变 |
| S5 改名失败 | `FAILED` | `SWAP_FAILED` | 未创建 | 删除 | — | 不变 |
| S5 后、S6 前取消 | `ROLLED_BACK` | `CANCELLED` | 本 job 目标删除 | 已清 | — | 不变 |
| S6 改名失败（源完整、无 tombstone） | `ROLLED_BACK` | `SWAP_FAILED` | 本 job 目标删除 | 已清 | — | 不变 |
| S6 状态不明（源与 tombstone 并存等） | `RECOVERY_REQUIRED` | `RECOVERY_REQUIRED` | 不删 | 不删 | 不删 | 不删 |
| S7 建链失败 | `ROLLED_BACK` | `LINK_FAILED` | 本 job 目标删除 | 已清 | 还原为源 | 数据完整 |
| S7 发现源路径被非本 job 链接占用 | `RECOVERY_REQUIRED` | `RECOVERY_REQUIRED` | 不删 | 不删 | 不删 | 不删 |
| S8 验证失败 | `ROLLED_BACK` | `LINK_VERIFY_FAILED` | **保留**（已有完整副本） | 已清 | 还原为源 | 数据完整、链接已拆 |
| S9 删除 tombstone 失败 | `LINKED_CLEANUP_PENDING` | `CLEANUP_FAILED` | 保留 | 已清 | 保留待重试 | 链接可用 |

要点：

- S8 失败时**保留最终目标**是架构 §6.3 的明确要求：链接已拆、源已还原，最终目标是额外的完整副本，删除它只会减少数据安全。
- 取消在 S6 之后、S8 通过之前**不生效**（架构 §6.8）；链接验证通过后清理总会执行完，避免留下"链接可用但 tombstone 永远残留"的中间态。

## 7. 恢复矩阵（`recover_job`）

恢复先观察五个路径（源、tombstone、最终、暂存、所有权标记），再结合 journal 的 `recorded_state` 分派。**观察优先，记录只用于消歧**；两种解释都成立时进入 `RECOVERY_REQUIRED`，不删任何东西。

| 观察到的状态 | recorded_state | 动作 | 结果 |
|---|---|---|---|
| 源=普通目录，tombstone 无，最终无 | `PLANNED/BACKED_UP/COPYING/COPIED/COPY_VERIFIED` | 删除本 job 暂存 | `FAILED` |
| 源=普通目录，tombstone 无，最终无 | `TARGET_READY` | 删除暂存 | `RECOVERY_REQUIRED`（记录与事实矛盾） |
| 源=普通目录，最终=本 job 目录 | `TARGET_READY` 或换位前状态 | 删除目标与暂存 | `FAILED`（源从未移动） |
| 源=普通目录，最终=本 job 目录 | `SOURCE_RENAMED` / `LINKED` | 删除目标（`SOURCE_RENAMED`） | `ROLLED_BACK` |
| 源=普通目录，最终=本 job 目录 | `ROLLED_BACK` | 保留目标 | `ROLLED_BACK`（上次回滚已停在正确位置） |
| 源=普通目录，tombstone 也存在 | 任意 | 不猜 | `RECOVERY_REQUIRED` |
| 源=普通目录，最终存在但无本 job 标记 | 任意 | 不删 | `RECOVERY_REQUIRED` |
| 源缺失，tombstone=目录，无链接 | 不在 `VERIFIED_LINK/DONE/LINKED_CLEANUP_PENDING` | 还原源；如有本 job 目标则删除；清暂存 | `ROLLED_BACK` |
| 源缺失，tombstone=目录，无链接 | 上述"已链接"状态 | 不猜 | `RECOVERY_REQUIRED` |
| 源=链接，目标匹配，最终为本 job 目录，验证通过 | 任意 | 验证链接、删 tombstone、清暂存 | `DONE` |
| 源=链接，验证通过但 tombstone 删除失败 | 任意 | 保留 tombstone | `LINKED_CLEANUP_PENDING` |
| 源=链接，验证失败，tombstone 仍在 | 任意 | 拆本 job 链接、还原源、清暂存 | `ROLLED_BACK` |
| 源=链接，验证失败，tombstone 缺失 | 任意 | 保留链接（唯一数据入口） | `RECOVERY_REQUIRED` |
| 源=链接，目标不匹配 / 最终无标记 | 任意 | 不删 | `RECOVERY_REQUIRED` |
| 源、tombstone、最终都缺失 | 任意 | 不删 | `RECOVERY_REQUIRED` |
| tombstone 不是目录 / 源既非目录也非链接 | 任意 | 不删 | `RECOVERY_REQUIRED` |

恢复过程发出 `recover:start` 与 `recover:done|failed` 事件，`failed` 仅对应 `RECOVERY_REQUIRED`。

## 8. 幂等性

| 操作 | 重复执行的行为 |
|---|---|
| `ensure_target_ready` | 最终已带本 job 标记 → 不做改名，只清理残留暂存 |
| `ensure_source_renamed` | 源已不在、tombstone 在 → 返回 `False`，不再改名 |
| `restore_source` | 源已是普通目录 → 返回 `False`，不覆盖 |
| `ensure_link` | 源已是匹配链接 → 返回 `(False, target)` |
| `remove_own_link` | 链接不存在 → `False`；指向别处 → 拒绝 |
| `remove_tombstone` / `remove_tree` | 路径不存在 → 空错误列表 |
| `recover_job` | 第二遍观察到已恢复/已完成状态，返回相同终态且无删除副作用 |

沙箱演练 `crash_before_link` 与 `crash_after_link` 都连续执行了两遍 `recover_job`，第二遍状态不变、数据不变。

## 9. 进度事件

`progress_cb` 收到统一结构：

```json
{"stage": "swap|link|link_verify|cleanup|recover",
 "status": "start|progress|done|failed",
 "job_id": "...", "tool_id": "...", "elapsed_seconds": 0.12, "...": "阶段字段"}
```

- `swap`：`start` 带源/最终/暂存路径；S5 后 `progress`（`step=S5, state=TARGET_READY`）；S6 后 `done`（`state=SOURCE_RENAMED, tombstone_path`）。
- `link`：`done` 带 `target / created`；失败带 `error_code=LINK_FAILED`。
- `link_verify`：`done` 带 `target / probe_rel / probe_kind`；失败带 `reason`。
- `cleanup`：成功 `state=DONE`；tombstone 删除失败 `state=LINKED_CLEANUP_PENDING`。
- `recover`：`start` 带 `recorded_state`；结束带 `state / message / observed`。

成功路径固定顺序：

```
swap:start → swap:progress(S5) → swap:done(S6)
→ link:start → link:done
→ link_verify:start → link_verify:done
→ cleanup:start → cleanup:done
```

## 10. 沙箱演练（真实 robocopy + 真实 junction）

脚本：`python tools/migrate_drill_b.py`。所有读写都在系统临时目录内完成，**不触碰真实 `%APPDATA%` / `%LOCALAPPDATA%`**。

演练树与 T09 一致：`Cursor/Cache/`（2 个数据文件，1,572,864 字节，含空目录 `empty/`）+ `Cursor/User/settings.json`、`keybindings.json`。

| 演练 | 注入方式 | 期望 | 实测 |
|---|---|---|---|
| `success` | 真实 robocopy + 真实 `mklink /J` | `DONE`；源变 junction；读回目标解析到最终目录；经链接读文件一致；tombstone/暂存删除；四阶段事件齐全 | 通过；读回值为 8.3 短路径 `...\ADMINI~1\...\demo\Cache`，`link_matches_final=true`；探测读 `level1/data.bin` 一致；事件 `swap → link → link_verify → cleanup` |
| `link_failure` | 仅 `create_link` 抛 `OSError` | `ROLLED_BACK` / `LINK_FAILED`；源恢复为普通目录且数据一致；目标、tombstone、暂存全部移除 | 通过；`source_restored / target_removed / tombstone_removed / staging_removed` 均为 true |
| `crash_before_link` | 真实准备 + S5 + S6 后直接调用 `recover_job(SOURCE_RENAMED)` | 第一遍 `ROLLED_BACK`（源还原、目标删除、tombstone 清除）；第二遍仍 `ROLLED_BACK` 且数据不变 | 通过；两次恢复均为 `ROLLED_BACK` |
| `crash_after_link` | 真实准备 + S5 + S6 + S7 后直接调用 `recover_job(LINKED)` | 第一遍 `DONE`（验证链接、删 tombstone、清暂存）；第二遍 `DONE` 且链接与数据保持不变 | 通过；两次均为 `DONE`，`link_verified=true` |

演练过程中发现并修复了一个真实缺陷：便携层 `src/platform/link.py` 的 `same_volume()` 引用了未定义的 `volume_info`，在真实平台适配器下必然 `NameError`。现改为调用时按平台延迟解析 `win/posix` 适配器，避免包初始化循环。

## 11. 测试与验收

### 11.1 测试结果

```
python -m unittest tests.test_migrate_complete tests.test_migrate_recover -v
# Ran 28 tests ... OK

python -m unittest discover -s tests -v
# Ran 260 tests ... OK

python tools/migrate_drill_b.py
# 4 个沙箱演练全部通过，退出码 0

python tools/migrate_drill.py
# T09 的 3 个演练无回归，退出码 0
```

单元测试覆盖：

- 成功路径：`DONE`、junction、读回目标、tombstone/暂存清除、所有步骤记录；
- 进度事件顺序与字段；
- 工具运行中 / 进程状态未知：`PROCESS_BUSY`，源不动、暂存清理；
- 未校验输入：`INVALID_RESULT`，暂存保留、源不动；
- 目标无本 job 标记：`SWAP_FAILED`，源不动；
- 建链失败：`ROLLED_BACK` / `LINK_FAILED`，源数据完整、目标与 tombstone 清除；
- 源改名失败：`ROLLED_BACK` / `SWAP_FAILED`，回到迁移前状态；
- 链接验证失败：`ROLLED_BACK` / `LINK_VERIFY_FAILED`，源还原、最终目标**保留**；
- 清理失败：`LINKED_CLEANUP_PENDING`，随后 `recover_job` 补完为 `DONE`；
- S5 前取消：`CANCELLED`，仅清理暂存；
- 恢复矩阵：§7 全部行，含幂等重跑、错误链接不删除、源与 tombstone 并存拒绝猜测。

### 11.2 验收对照

| 验收项 | 结果 |
|---|---|
| 只有校验通过才删除源目录 | `complete_migration` 首先要求 `result.ok && verify.ok`；源只做原子改名，真正的删除仅发生在 S8 通过后的 tombstone 清理 |
| 建链后能读回链接目标并确认指向目标盘 | `verify_link` 用 `read_junction_target` + `link_target_matches`（兼容 8.3/`\\?\`）比较，再经链接探测读文件；演练用真实 `mklink` 验证 |
| 任一步失败自动恢复到迁移前状态 | §6 矩阵；S5/S6/S7 回滚到"数据在、链接无"；S8 回滚保留额外副本；证据不足转 `RECOVERY_REQUIRED` 不删 |
| 恢复动作幂等，可重复执行无副作用 | §8；单测与两个崩溃演练均连续执行两遍恢复，状态与数据不变 |
| 进度事件覆盖建链与验证阶段 | `swap / link / link_verify / cleanup / recover` 全部发出 `start/progress/done/failed` |

## 12. 已知限制与风险

1. **journal 尚未落盘**：`recover_job` 的 `recorded_state` 由调用方提供，持久化时机与格式属于 T11。缺少记录时，恢复只在观察唯一可解释时动作，否则 `RECOVERY_REQUIRED`。
2. **S8 回滚保留最终目标**：这是有意的数据安全选择，会暂时出现"源与目标各一份完整数据"。需要 T11 在记录中标注 `ROLLED_BACK`，由保留策略决定何时清理。
3. **junction 仅 Windows**：`mklink /J` 要求 NTFS；ReFS/FAT 或跨卷目标需在计划阶段拒绝。macOS 使用目录符号链接，等价实现由 T21 完整验证。
4. **进程清单来源**：`process_names` 由目录清单提供；为空表示"清单未定义进程名"，不阻塞；枚举失败一律按阻塞处理（`unknown`）。
5. **恢复的删除边界**：`remove_tree` 要求父目录、命名模式和所有权标记三重校验；源路径永远不传给清理函数。若用户手工在 `.dw-partial-<job8>` 里放了数据，恢复会按"本 job 临时目录"删除——这是命名约定下的预期行为。
6. **tombstone 与源同盘**：要求同父目录同卷，因此目标盘可以是另一块盘，但换位源本身不跨盘。
7. **真实用户数据未演练**：按任务约束只在临时沙箱造数，没有对真实 IDE 缓存执行迁移。
8. **清理失败只报告不重试**：`LINKED_CLEANUP_PENDING` 需由启动恢复或用户动作再次触发 `recover_job`，本任务不在进程内循环重试。

## 13. 下一步建议（T11）

1. 定义 journal 记录（job_id、tool_id、源/最终/tombstone、当前状态、时间戳），在 S4–S9 每个状态边界落盘，供启动恢复直接传入 `recorded_state`。
2. 启动时扫描未终止 job：调用 `recover_job`，把 `RECOVERY_REQUIRED` 暴露到 UI，而不是自动猜测。
3. 迁移历史：记录成功/回滚/待清理与备份目录，按 job8 关联 T09 的配置备份。
4. 由 app 层注入取消 token 与进度回调，把 `link_verify` 的探测结果展示给用户。
5. 备份与 `LINKED_CLEANUP_PENDING` 的保留/清理策略（按时间、空间与用户确认）。
6. 在真实 NTFS 大盘上做一次非用户数据的压力演练，观察在十万级文件时 S5/S6 改名与恢复的耗时。
