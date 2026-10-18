# 决策 0007：macOS 构建使用 GitHub Actions `macos-latest`

- 日期：2026-10-19
- 决策人：项目领导人（依据人类在 QUOT-2 的指示）
- 状态：已生效

## 背景

T21（macOS 适配与打包）需要 macOS 环境才能产出 `.app`。人类在 QUOT-2 确认 D5：没有现成 Mac，改用 GitHub Actions 的 `macos-latest` runner 打包。

## 决策

1. **macOS 构建统一走 GitHub Actions `macos-latest`**，不要求团队持有 Mac 机器。
2. **最小构建工作流并入 T21**：`.github/workflows/build-macos.yml`，在 `macos-latest` 上安装 Python、构建 PyInstaller onedir 产物、压缩为 zip 并上传 artifact；触发方式先支持手动 `workflow_dispatch`，T19 再补 tag/发布触发。
3. **需要人类提供远端仓库**：GitHub 仓库地址与可见性（公开/私有）。没有远端仓库时 T21 只能完成代码与本地校验，无法产出 `.app`。
4. **签名与公证不在本决策范围**：首期交付未签名 `.app` + 首次启动引导（决策 0005）。若要消除 Gatekeeper 警告，仍需 Apple Developer 账号（约 $99/年），由人类后续决定。
5. **额度提示**：公开仓库的标准 runner（含 macOS）免费；私有仓库在 GitHub Free 下每月 2,000 分钟额度，macOS 按 10 倍计费，约合 200 分钟/月，足够本项目手动构建。
6. **排期不变**：T21 仍依赖 T10、T12、T15；T19（通用 CI 与发布）保持 P2，不因本决策提前。

## 影响

- `任务拆解清单` T21 交付物增加 `.github/workflows/build-macos.yml`，依赖增加"人类提供 GitHub 远端仓库"。
- `派活清单` D5 置为"已确认：GitHub Actions macOS runner"，并新增"待人类提供仓库地址"。
- 本机（Windows）不再承担 macOS 构建验证；macOS 行为验证以 CI 日志 + artifact 为准，必要时由人类在任意 Mac 上做一次冒烟。

## 相关

- `docs/decisions/0005-Python版本基线与macOS首次启动引导.md`
- `docs/tasks/TASK-001/任务拆解清单.md`（T21、T19）
- `docs/tasks/TASK-001/派活清单.md`（D5、QUOT-20）
