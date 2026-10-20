# Windows 开发版打包说明

本目录只放打包脚本，不改动 `src/`、`ui/` 的业务代码。产物是**未签名、未安装**的
PyInstaller onedir 目录，压缩为 `DiskWarden-<version>-win-x64.zip`，解压后双击
`DiskWarden\DiskWarden.exe` 即可启动。

## 产物构成

- 入口：`packaging/launcher.py` → `src.app.main`（pywebview + WebView2 主界面）
- 根目录附带 `DiskWarden.exe.config`：从浏览器下载的 zip 解压后所有文件带
  Mark-of-the-Web，.NET Framework 默认拒绝 `Assembly.LoadFrom` 加载
  `pythonnet/runtime/Python.Runtime.dll`，表现为
  `Failed to resolve Python.Runtime.Loader.Initialize`；该配置允许 load-from-remote
- `ui/index.html`、`ui/assets/`（品牌图标）与 `data/ide-catalog.json`、`data/rules.json`
  被打进应用根目录，冻结后仍沿用 `src/app` 里的 `REPO_ROOT` 查找逻辑
- 版本资源：`packaging/version_info.txt`（当前 `0.9.0-beta.5`），exe 图标：`packaging/DiskWarden.ico`
- 本版不做安装器、不做代码签名，也不包含自动更新（见决策 0008 / T15）

## 构建环境

- Windows 10/11 x64，Python **3.12**（决策 0012；取代决策 0005 的 3.8 基线）
- 依赖版本锁定在 `requirements-build.txt`，PyInstaller 6.x + hooks-contrib 2026.8
- 目标机器需要 WebView2 Runtime（Win11 与已更新的 Win10 通常自带），不需要 Python
- PyInstaller 6 把运行库收进 `_internal/`；**不要**把新包解压覆盖到旧版目录，
  否则新旧 `python3XX.dll` 混在一起会报
  `Module use of pythonXX.dll conflicts with this version of Python`。
  启动器会先检查这种残留并给出明确提示（退出码 3）

```powershell
py -3.12 -m venv .venv-build
.\.venv-build\Scripts\python.exe -m pip install -r packaging\requirements-build.txt
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Python .\.venv-build\Scripts\python.exe
```

脚本会清理旧的 `build/`、`dist/DiskWarden` 与同名 zip，重新构建并输出体积与 SHA256。

## 手动构建（等效命令）

```powershell
python -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging\DiskWarden.spec
Compress-Archive -Path dist\DiskWarden -DestinationPath dist\DiskWarden-0.9.0-beta.5-win-x64.zip
Get-FileHash -Algorithm SHA256 dist\DiskWarden-0.9.0-beta.5-win-x64.zip
```

## 验证清单

1. 在未安装 Python 的机器上解压，双击 `DiskWarden.exe`，主界面出现且无控制台窗口。
2. 点击「重新扫描」，扫描页列出本机真实 IDE 目录与体积；扫描只读，不触发迁移。
3. 关闭窗口后进程退出；日志位于数据根目录的 `logs/app.log`（选择顺序见下节）。
4. 首次运行可能出现 SmartScreen 提示（未签名），按 T15 的说明走「更多信息 → 仍要运行」。

## 数据根目录（启动时选择）

`launcher.py` 在加载主程序前逐个探测候选目录，选第一个**可写**的作为
`DISKWARDEN_DATA_ROOT`，因此 `D:` 不可写或被安全软件拦截时不会静默退出：

1. `DISKWARDEN_DATA_ROOT`（显式覆盖）
2. 平台默认目录（Windows 为 `D:\DiskWarden`）
3. `%LOCALAPPDATA%\DiskWarden`
4. `%APPDATA%\DiskWarden`
5. `%USERPROFILE%\DiskWarden`
6. `%TEMP%\DiskWarden`

全部不可写时弹出错误框（无控制台的窗口版也能看到），列出已尝试的路径后退出。
同一机器多次启动应保持同一根目录；若根目录临时不可写而落到下一项，日志会出现在新位置。

## 注意事项

- **不要提交** `dist/`、`build/`、`.venv-build/`、zip；`.gitignore` 已覆盖。
- 迁移/回滚验证只能使用临时目录，不要对真实用户数据做演练。
- 升级 PyInstaller 或 pythonnet 后要重新验证 `webview.platforms.edgechromium` 的
  DLL 收集（`webview/lib/` 由 hooks-contrib 处理）。
- 应用内 `APP_VERSION` 由 `src/app/__init__.py` 提供（当前 `0.9.0-beta.5`），
  同时写在 exe 版本资源与 zip 文件名上；`build.ps1 -Version` 默认值与此保持一致。
