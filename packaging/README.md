# Windows 开发版打包说明

本目录只放打包脚本，不改动 `src/`、`ui/` 的业务代码。产物是**未签名、未安装**的
PyInstaller onedir 目录，压缩为 `DiskWarden-<version>-win-x64.zip`，解压后双击
`DiskWarden\DiskWarden.exe` 即可启动。

## 产物构成

- 入口：`packaging/launcher.py` → `src.app.main`（pywebview + WebView2 主界面）
- `ui/index.html` 与 `data/ide-catalog.json`、`data/rules.json` 被打进应用根目录，
  冻结后仍沿用 `src/app` 里的 `REPO_ROOT` 查找逻辑
- 版本资源：`packaging/version_info.txt`（默认 `0.1.0-dev`）
- 本版不做安装器、不做代码签名，也不包含自动更新（见决策 0008 / T15）

## 构建环境

- Windows 10/11 x64，Python **3.8–3.11**（决策 0005 的 3.8+ 基线）
- 依赖版本锁定在 `requirements-build.txt`，PyInstaller 5.x + hooks-contrib 2023.6
- 目标机器需要 WebView2 Runtime（Win11 与已更新的 Win10 通常自带），不需要 Python

```powershell
py -3.8 -m venv .venv-build
.\.venv-build\Scripts\python.exe -m pip install -r packaging\requirements-build.txt
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Python .\.venv-build\Scripts\python.exe
```

脚本会清理旧的 `build/`、`dist/DiskWarden` 与同名 zip，重新构建并输出体积与 SHA256。

## 手动构建（等效命令）

```powershell
python -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging\DiskWarden.spec
Compress-Archive -Path dist\DiskWarden -DestinationPath dist\DiskWarden-0.1.0-dev-win-x64.zip
Get-FileHash -Algorithm SHA256 dist\DiskWarden-0.1.0-dev-win-x64.zip
```

## 验证清单

1. 在未安装 Python 的机器上解压，双击 `DiskWarden.exe`，主界面出现且无控制台窗口。
2. 点击「重新扫描」，扫描页列出本机真实 IDE 目录与体积；扫描只读，不触发迁移。
3. 关闭窗口后进程退出；日志位于数据根目录的 `logs/app.log`
   （默认 `%LOCALAPPDATA%\DiskWarden`，若存在 `bootstrap.json` 则以其中 `data_root` 为准）。
4. 首次运行可能出现 SmartScreen 提示（未签名），按 T15 的说明走「更多信息 → 仍要运行」。

## 注意事项

- **不要提交** `dist/`、`build/`、`.venv-build/`、zip；`.gitignore` 已覆盖。
- 迁移/回滚验证只能使用临时目录，不要对真实用户数据做演练。
- 升级 PyInstaller 或 pythonnet 后要重新验证 `webview.platforms.edgechromium` 的
  DLL 收集（`webview/lib/` 由 hooks-contrib 处理）。
- 应用内 `APP_VERSION` 仍由 `src/app/__init__.py` 提供（当前 `0.2.0`）；`0.1.0-dev`
  是本次点检产物的构建版本号，写在 exe 版本资源与 zip 文件名上。
