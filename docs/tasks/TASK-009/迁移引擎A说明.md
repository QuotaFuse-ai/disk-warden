# T09 迁移引擎 A 说明：配置备份、搬运与校验

- 任务：QUOT-11 / T09
- 日期：2026-10-19
- 负责人：主机端开发
- 状态：已实现，待验收
- 上游依据：`docs/specs/架构设计-v1.0.md` §4.4/§6.1/§6.2/§6.3/§6.6、`docs/decisions/0004-第一期范围修订-无官网与双平台客户端.md`、`docs/decisions/0005-Python版本基线与macOS首次启动引导.md`、T07/T08 交付物

## 1. 范围与结论

本任务实现迁移的前半段 **S1–S4**：

1. **S1 配置备份**：只备份已知的小配置文件，逐文件 SHA-256，manifest 最后原子写入；
2. **S2–S3 搬运**：业务层调用平台层 `run_robocopy`，把源复制到本 job 专属暂存目录；
3. **S4 校验**：相对路径集合 + 每文件字节 + 文件数 + 总字节全部一致才算通过。

结论：

- 源数据在校验通过前**只读**；任何失败/取消都只清理本 job 的暂存目录，**绝不删除或重命名源**。
- robocopy 退出码 `0–7` 视为成功，`>= 8` 一律失败并中止（不使用 `/MIR`）。
- 校验同时比对**文件数**与**总字节**，并额外比对相对路径集合与逐文件大小；任一侧出现读取/权限错误即判失败。
- 暂存与备份目录名都内嵌 `job8`，清理前校验父目录与命名模式，杜绝按通配符删除用户数据。
- 阶段化进度事件覆盖 `prepare / backup / copy / verify`；复制期进度是采样值，校验才是权威结果。
- 本任务在 Windows 上用**真实 robocopy** 完成 1 次成功 + 2 次故意失败的沙箱演练，结果见 §10。
- 新增 23 个单元测试，全套 110 个测试全部通过。

本任务**不包含**换位（暂存改名为最终目标）、删源、建链、回滚、journal 与恢复矩阵，这些属于 T10/T11。

## 2. 产出文件

| 文件 | 作用 |
|---|---|
| `src/core/migrate/models.py` | 请求/结果、阶段与错误码、备份 manifest、校验结果模型 |
| `src/core/migrate/paths.py` | 最终/暂存/备份命名、`job8` 校验、路径包含关系判断 |
| `src/core/migrate/backup.py` | 配置发现、大小上限、SHA-256、manifest 原子写入（S1） |
| `src/core/migrate/copy.py` | 调用平台搬运原语、暂存采样进度、退出码结果（S2–S3） |
| `src/core/migrate/verify.py` | V2 校验：路径集合、逐文件字节、文件数、总字节（S4） |
| `src/core/migrate/cleanup.py` | 仅删除本 job 命名目录，reparse point 只删链接不进入 |
| `src/core/migrate/engine.py` | `prepare_copy()` 编排 S1–S4、失败码与清理 |
| `src/core/migrate/__init__.py` | 包入口与公开 API |
| `src/platform/transfer.py` | 平台无关搬运契约与异常 |
| `src/platform/win/transfer.py` | Windows robocopy 适配器（唯一 `subprocess` 调用点） |
| `src/platform/__init__.py` | 增加搬运原语导出 |
| `tests/test_migrate_backup.py` | 6 个备份测试 |
| `tests/test_migrate_verify.py` | 6 个校验测试 |
| `tests/test_migrate_engine.py` | 11 个编排/失败/取消测试 |
| `tools/migrate_drill.py` | 真实 robocopy 沙箱演练脚本 |
| `docs/tasks/TASK-009/迁移引擎A说明.md` | 本文档 |

### 关于目录位置

issue 的交付物写作 `src/migrate/`，冻结架构（`docs/specs/架构设计-v1.0.md` §3.3）把迁移层定位为 `core/migrate`。本任务按架构落到 `src/core/migrate/`，与 T06–T08 的 `src/core/scan/`、`src/core/rules/` 保持一致。包内只用相对导入；如确需回到 `src/migrate/`，移动整个包即可。

## 3. 分层与调用关系

```
engine.prepare_copy
  ├─ backup.backup_config        # 纯标准库，配置备份
  ├─ copy.copy_to_staging
  │     └─ platform.run_robocopy # 业务层不直接碰 subprocess
  └─ verify.verify_trees         # 纯标准库
```

- `src/core/migrate/` 不导入 `subprocess` / `ctypes`，robocopy 参数与进程管理集中在 `src/platform/win/transfer.py`；
- 非 Windows 平台上 `run_robocopy` 抛出 `TransferUnsupportedError`。macOS 的搬运实现按决策 0004 由 T21 负责，本任务不提供假实现。

## 4. 步骤与命名

```
最终目标：<target_root>/<tool_id>/<basename>
暂存目录：<target_root>/<tool_id>/.dw-partial-<job8>/<basename>
备份目录：<backup_root>/<yyyy-MM-dd_HHmmss>_<tool_id>_<job8>/
```

- 暂存与最终目标同卷，T10 可直接 `os.rename`；
- `job_id` 必须是 8–32 位十六进制；`job8` 取前 8 位；
- `tool_id` 只允许字母、数字、`.`、`_`、`-`，且不能是 `.` / `..`；
- `target_root` 必须已存在；源、目标根、暂存/最终路径两两不得互相包含（含相等），避免把数据复制进自己；
- 源本身是 reparse point 时直接拒绝（`PREPARE_FAILED`）。

## 5. S1 配置备份

### 5.1 选文件

- 默认只识别：`settings.json`、`settings.json.backup`、`keybindings.json`、`argv.json`、`storage.json`、`extensions.json`。
- 从 `config_root` 向下最多 3 层目录；`config_root` 默认等于 `source_path`。
- 单个配置文件上限 256 KiB；超限记为 `too_large` 跳过，不中断。
- reparse point 一律不跟随；命中配置名时记为 `reparse_point` 跳过。
- 调用方也可传 `config_files`（绝对路径或相对 `config_root`）：显式文件不存在或不是普通文件时**直接失败**，避免"以为备份了其实没备份"。

> 实践提示：Cursor 的配置在 `Cursor/User/`，而被迁移的 `Cursor/Cache/` 是它的兄弟目录。调用方应把 `config_root` 指向 `Cursor/`，`source_path` 仍指向 `Cache/`，否则发现不到配置。`MigrationRequest` 已为此拆出 `config_root` 字段。

### 5.2 写备份

- 目录结构：`<backup_dir>/files/<相对路径>` + `<backup_dir>/manifest.json`；
- 每个文件边复制边计算 SHA-256，并校验读到的字节数与目标文件大小；
- manifest 记录 `schema_version / job_id / tool_id / source_root / backup_dir / created_at / entries[] / skipped[]`；
- manifest 最后写入：先写 `manifest.json.tmp`，再 `os.replace` 原子替换，因此 **manifest 存在即代表备份完整**；
- S1 失败时删除本次 job 的备份目录（命名+job8 双重校验），源不动。

## 6. S2–S3 搬运

- `run_robocopy(src, dst, log_path, cancel_token) -> exit_code`，以下参数固定：
  `/E /COPY:DAT /DCOPY:T /XJ /R:2 /W:2 /NP /NFL /NDL /NJH /NJS /BYTES /LOG:<path>`
- 不使用 `/MIR`（会删除目标端多余文件），使用 `/E`（包含空目录）；
- `/XJ` 明确不跟随 junction；默认有界重试 `/R:2 /W:2`；
- 业务层只判断原始退出码：`0 <= code < 8` 成功，`>= 8` 失败；
- 进程以 `CREATE_NO_WINDOW` 启动，不弹黑框；取消时终止该 robocopy 进程并等待最多 5 秒；
- 复制前创建 `.dw-partial-<job8>`；该目录已存在时返回 `COPY_FAILED`，不覆盖任何既有目录；
- 进度：每 2 秒采样一次暂存目录的字节数/文件数；文件数超过 20 万时采样间隔逐步放大到最多 10 秒。采样失败被忽略，校验负责兜底。

## 7. S4 校验（V2）

两侧都用 `os.scandir` 迭代遍历：

- 相对路径集合一致（大小写按平台规范化）；
- 每个相对路径的文件大小一致；
- 文件数一致；
- 总字节一致；
- 任一侧出现权限/读取错误 → 校验失败（宁可失败，不可误删源）；
- reparse point 只记录路径、不进入；
- 空目录不参与比对（`/E` 会复制，但空目录不承载数据）。

失败时返回 `VERIFY_FAILED`，附 `missing / extra / size_mismatches / reparse_paths / errors` 与两侧计数、字节数，并删除本 job 的暂存目录；**源始终不变**。

不做全量 SHA-256：几十 GB 数据的哈希会让迁移时间翻倍，P0 只要求 V2；SHA-256 仅用于配置备份（V3）。

## 8. 进度事件

`progress_cb` 收到统一结构：

```json
{"stage": "prepare|backup|copy|verify", "status": "start|progress|done|failed",
 "job_id": "...", "tool_id": "...", "elapsed_seconds": 0.12, "...": "阶段字段"}
```

- `prepare`：`source_path / final_path / staging_path`；
- `backup`：`backup_dir / files_done / bytes_done`；
- `copy`：`staging_path / exit_code / bytes_done / files_done`，可选的 `bytes_total / files_total` 只在调用方提供期望值时出现；
- `verify`：`source_file_count / target_file_count / source_bytes / target_bytes`。

成功路径的事件顺序固定为：

```
prepare:start → prepare:done → backup:start → backup:done
→ copy:start → copy:[progress…] → copy:done
→ verify:start → verify:done
```

失败路径把对应阶段改为 `failed` 并附 `message`。

## 9. 失败、取消与清理

| 情况 | 结果 | 暂存 | 备份 | 源 |
|---|---|---|---|---|
| 前置校验失败 | `PREPARE_FAILED` | 未创建 | 未创建 | 不变 |
| 备份失败 | `BACKUP_FAILED` | 未创建 | 删除本 job 目录 | 不变 |
| robocopy 启动失败/退出码 ≥ 8 | `COPY_FAILED` | 删除 | 保留 | 不变 |
| 校验不一致/读错误 | `VERIFY_FAILED` | 删除 | 保留 | 不变 |
| 取消 | 抛 `MigrationCancelled` | 删除 | 保留 | 不变 |

- 取消不是失败结果：调用方按"作业已放弃"处理，不写失败历史；
- 取消发生在复制中时，备份保留（属于安全资产，可按 job8 定位清理）；
- `remove_tree()` 只接受"父目录 = 期望父目录 且 名称匹配本 job"的目录；根本身是 reparse point 时拒绝删除；子项是 reparse point 时只删链接，不进入目标；
- 源路径从未传给清理函数。

## 10. 沙箱演练

脚本：`python tools/migrate_drill.py`。所有读写都在系统临时目录内完成，**不触碰真实 `%APPDATA%` / `%LOCALAPPDATA%`**。

演练树：`Cursor/Cache/`（2 个数据文件，1,572,864 字节，含空目录 `empty/`）+ `Cursor/User/settings.json`、`keybindings.json`。

| 演练 | 注入方式 | 期望 | 实测 |
|---|---|---|---|
| 成功 | 真实 robocopy | `ok=true`，4 阶段完成，源不变，备份 2 个配置 + manifest | 通过；robocopy 退出码 1；校验文件 2/2、字节 1,572,864/1,572,864 |
| 校验失败 | 真实 robocopy 后向暂存文件追加 16 字节 | `VERIFY_FAILED`，暂存删除，备份保留，源不变 | 通过；目标字节 1,572,880，`size_mismatch_count=1` |
| 搬运失败 | 真实 robocopy 指向一个"其实是文件"的目标 | 退出码 ≥ 8，`COPY_FAILED`，暂存删除，源不变 | 通过；robocopy 真实退出码 16 |

成功演练还断言了：事件顺序 `prepare → backup → copy → verify`、暂存内文件与空目录齐全、备份文件 SHA-256 与 manifest 一致、robocopy 日志存在、`final_path = <target_root>/demo/Cache`。

## 11. 测试与验收

### 11.1 测试结果

```
python -m unittest tests.test_migrate_backup tests.test_migrate_verify tests.test_migrate_engine -v
# Ran 23 tests ... OK

python -m unittest discover -s tests -v
# Ran 110 tests ... OK

python tools/migrate_drill.py
# 3 个沙箱演练全部通过，退出码 0
```

### 11.2 验收对照

| 验收项 | 结果 |
|---|---|
| 只备份配置类文件 | 白名单 + 3 层深度 + 256 KiB 上限；reparse/超限跳过并记原因 |
| robocopy 退出码 0–7 成功、≥8 失败即中止 | `CopyResult.ok = 0 <= code < 8`；单测覆盖 8、演练覆盖真实 16 |
| 校验同时比对文件数与总字节，任一不符不删除源 | V2 四处比对（路径集合/逐文件/计数/总字节）；失败只删暂存；单测与演练均断言源不变 |
| 阶段化进度事件（准备/备份/复制/校验） | 四阶段 `start/progress/done/failed`，单测断言成功路径 8 个事件 |
| 沙箱至少一次成功 + 一次故意失败 | 成功 1 次 + 故意失败 2 次（校验失败、搬运失败），见 §10 |
| 业务层不直接调用 robocopy | `src/core/migrate/` 无 `subprocess`；唯一调用点在 `src/platform/win/transfer.py` |

## 12. 已知限制与风险

1. **只做前半段（S1–S4）**：暂存目录完整且已校验，但源尚未换位，最终目标尚不存在；`final_path` 只是 T10 的目标位置。
2. **未实现 journal 与断点恢复**：本任务的清理只针对"当前进程内的失败"。进程崩溃后残留的 `.dw-partial-<job8>` 需要 T10/T11 的启动恢复逻辑处理；命名中带 job8 就是为了让恢复能证明归属。
3. **未实现同 job 重入**：暂存目录已存在时直接 `COPY_FAILED`，不做 robocopy 增量续跑。若 T10 需要"ensure_copy"幂等，应在恢复层决定是复用还是先清理。
4. **取消靠终止进程**：Windows 下直接终止 robocopy 进程（无子进程树问题），未使用 psutil；终止后暂存可能含部分文件，由清理步骤删除。
5. **进度是采样值**：robocopy 运行中按暂存目录大小估算；最后以 S4 校验为准。`bytes_total/files_total` 只有在调用方预统计时才提供，本引擎不额外扫描源。
6. **macOS 搬运未实现**：`run_robocopy` 在非 Windows 抛 `TransferUnsupportedError`，由 T21 提供等价实现（如 `ditto`/`rsync`）并复用同一契约。
7. **T10 的 owner marker 与 V2 的冲突**：架构 §6.5 建议在暂存目录写 `.dw-owner-<job8>.json`。若 T10 这样做，必须在校验**之后**写入，或在后续校验中显式排除，否则会破坏文件数/总字节一致性。
8. **真实用户数据未演练**：按任务约束，本任务只在沙箱造数，没有对真实 IDE 缓存执行迁移。

## 13. 下一步建议（T10）

1. 以 `MigrationResult` 为输入：`staging_path` 已校验，直接执行同卷 `os.rename` 到 `final_path`；
2. 换位前再次检查进程占用（§6.1 第 8 条），并校验 `final_path` 不存在；
3. 实现 journal 与启动恢复矩阵，至少覆盖 `COPYING / COPIED / COPY_VERIFIED` 三种崩溃点；
4. 建链后读回 junction 目标并探测一个已知文件，再进入清理；
5. 为 T10 增加故障注入演练，断言"数据至少存在一份"。
