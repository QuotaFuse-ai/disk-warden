# -*- coding: utf-8 -*-
"""PyInstaller entry point.

The frozen application cannot use ``python -m src.app.main``, so this launcher
puts the repository root (``sys._MEIPASS`` when frozen) on ``sys.path`` and
delegates to the real host entry point.  It lives under ``packaging/`` on
purpose: no business code is duplicated here.

The launcher also picks the first writable data root before importing the
host.  A frozen, windowed build has no console: if every candidate fails the
user gets a message box instead of a silent exit, and an unusable
``D:\\DiskWarden`` or ``%LOCALAPPDATA%\\DiskWarden`` no longer aborts startup.

WebView2 problems are handled here too.  pywebview keeps its browser profile
in ``%APPDATA%\\pywebview``, which is shared with every other pywebview app and
can be left corrupt by an interrupted run; when WebView2 cannot open it the
form simply stays white.  The profile is relocated under the writable data
root, a missing WebView2 runtime is reported before the window opens, and an
initialization failure shows the real error and schedules a profile reset for
the next start instead of failing silently.
"""

import ctypes
import os
import shutil
import sys
import tempfile
import time
import traceback

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)

from src.core.store.layout import DataLayout, default_data_root  # noqa: E402

WEBVIEW_DIR = "webview"
WEBVIEW_FAILED_MARKER = ".webview-init-failed"

PYTHON_DLL_PREFIX = "python3"
PYTHON_DLL_SUFFIX = ".dll"


def _is_python_runtime_dll(name):
    # type: (str) -> bool
    """True for ``python3XX.dll``, never for the generic ``python3.dll``."""
    low = name.lower()
    if not (low.startswith(PYTHON_DLL_PREFIX) and low.endswith(PYTHON_DLL_SUFFIX)):
        return False
    return low[len(PYTHON_DLL_PREFIX):-len(PYTHON_DLL_SUFFIX)].isdigit()


def _bundled_python_dll():
    # type: () -> str
    """Name of the versioned runtime DLL shipped with this frozen build."""
    base = getattr(sys, "_MEIPASS", "") or ""
    if not base:
        return ""
    try:
        names = os.listdir(base)
    except OSError:
        return ""
    for name in names:
        if _is_python_runtime_dll(name):
            return name
    return ""


def _stale_python_dlls(app_dir, expected):
    # type: (str, str) -> list
    """Runtime DLLs at the exe root that this build did not ship.

    Extracting a PyInstaller 6 package over an older Python 3.8 onedir leaves
    ``python38.dll`` next to the new exe; the loader then fails with
    "Module use of python38.dll conflicts with this version of Python".
    """
    stale = []
    try:
        names = os.listdir(app_dir)
    except OSError:
        return stale
    for name in names:
        if not _is_python_runtime_dll(name):
            continue
        if expected and name.lower() == expected.lower():
            continue
        if os.path.isfile(os.path.join(app_dir, name)):
            stale.append(name)
    return sorted(stale)


def _check_runtime_layout():
    # type: () -> bool
    """Refuse to start when older runtime files were extracted over this build."""
    if not getattr(sys, "frozen", False):
        return True
    expected = _bundled_python_dll()
    app_dir = os.path.dirname(os.path.abspath(sys.executable))
    stale = _stale_python_dlls(app_dir, expected)
    if not stale:
        return True
    _message(
        "Disk Warden 检测到旧版本残留的运行库：\n\n%s\n\n"
        "这通常是因为把新版压缩包直接解压覆盖到了旧版目录。新旧 Python "
        "运行库混在一起时，程序会报 “Module use of python38.dll conflicts "
        "with this version of Python” 之类的错误。\n\n"
        "请把压缩包解压到一个全新的空目录（例如 C:\\DiskWarden），"
        "不要覆盖旧版本，然后重新打开。"
        % "\n".join(stale)
    )
    return False


def _fatal_exit(code):
    # type: (int) -> None
    """Terminate without unwinding; a normal window close can hang here."""
    sys.stderr.flush()
    os._exit(code)


def _message(text):
    # type: (str) -> None
    try:
        ctypes.windll.user32.MessageBoxW(None, text, "Disk Warden", 0x10)
    except Exception:
        sys.stderr.write(text + "\n")


def _redirect_logs(root):
    # type: (str) -> None
    """A windowed build has no stderr; keep the startup diagnostics on disk."""
    try:
        log_dir = os.path.join(root, "logs")
        os.makedirs(log_dir, exist_ok=True)
        stream = open(
            os.path.join(log_dir, "launcher.log"), "a", encoding="utf-8", buffering=1
        )
    except OSError:
        return
    sys.stdout = stream
    sys.stderr = stream
    sys.stderr.write(
        "\n=== %s launcher start, exe=%s, data_root=%s\n"
        % (time.strftime("%Y-%m-%d %H:%M:%S"), sys.executable, root)
    )


def _candidates():
    # type: () -> list
    seen = set()
    roots = []

    def add(path):
        # type: (object) -> None
        if not path:
            return
        resolved = os.path.abspath(os.path.expanduser(path))
        key = os.path.normcase(resolved)
        if key not in seen:
            seen.add(key)
            roots.append(resolved)

    env = os.environ
    local = env.get("LOCALAPPDATA") or ""
    roaming = env.get("APPDATA") or ""
    home = env.get("USERPROFILE") or os.path.expanduser("~")

    add(env.get("DISKWARDEN_DATA_ROOT"))
    # default_data_root() honours the override, so hide it while asking for the
    # platform default; a dead override must not remove D:\DiskWarden from the
    # fallback chain.
    saved = env.pop("DISKWARDEN_DATA_ROOT", None)
    try:
        add(default_data_root())
    finally:
        if saved is not None:
            env["DISKWARDEN_DATA_ROOT"] = saved
    add(os.path.join(local, "DiskWarden") if local else "")
    add(os.path.join(roaming, "DiskWarden") if roaming else "")
    add(os.path.join(home, "DiskWarden"))
    try:
        add(os.path.join(tempfile.gettempdir(), "DiskWarden"))
    except OSError:
        # gettempdir() raises when no temp candidate is writable (a Low
        # integrity start cannot create files under %TEMP%); that must not
        # abort startup before the configured data root has been tried.
        pass
    return roots


def _writable(root):
    # type: (str) -> bool
    try:
        return DataLayout(root).is_writable()
    except Exception:
        return False


def _prepare_data_root():
    # type: () -> bool
    tried = _candidates()
    for root in tried:
        if _writable(root):
            os.environ["DISKWARDEN_DATA_ROOT"] = root
            return True
    _message(
        "Disk Warden 无法启动：找不到可写的数据目录。\n\n"
        "已尝试：\n%s\n\n"
        "请确认磁盘可写，或删除被安全软件/权限占用的 DiskWarden 目录后重试。"
        % "\n".join(tried)
    )
    return False


def _warn_low_integrity():
    # type: () -> None
    """Explain a Low-integrity start before the user meets WinError 5.

    Windows starts an image at the integrity label of its file, so a package
    extracted into a Low-labelled folder runs Low even from an ordinary window
    and is denied every write under ``%APPDATA%``.  The token level is fixed at
    process creation, so this can be reported here but not repaired.
    """
    if not sys.platform.startswith("win"):
        return
    try:
        from src.platform.win.integrity import (
            MEDIUM,
            level_name,
            process_integrity_level,
        )

        level = process_integrity_level()
    except Exception:
        return
    if level is None or level >= MEDIUM:
        return
    sys.stderr.write(
        "low integrity start: token IL 0x%X (%s); writes to %%APPDATA%% and"
        " other Medium folders will be denied\n" % (level, level_name(level))
    )
    app_dir = os.path.dirname(os.path.abspath(sys.executable))
    _message(
        "Disk Warden 正在以%s完整性级别（0x%X）运行。\n\n"
        "Windows 的强制完整性策略禁止低完整性进程在 %%APPDATA%% 等普通目录中"
        "重命名、移动、新建子目录或删除已有内容，因此跨盘迁移会在切换"
        "源目录时失败并报 WinError 5，原因不是文件被占用。\n\n"
        "常见原因是程序被解压到了带 Low 完整性标签的文件夹。请关闭程序，"
        "把程序所在文件夹改为 Medium 后重新打开，例如在命令提示符中执行：\n\n"
        "icacls \"%s\" /setintegritylevel (OI)(CI)M /T\n\n"
        "也可以把程序解压到未加标签的普通目录（例如 C:\\DiskWarden）后重新打开。"
        % (level_name(level), level, app_dir)
    )


def _prepare_webview_profile():
    # type: () -> tuple
    """Return (profile dir, marker path) after recovering from a failed start.

    The profile never lives in ``%APPDATA%\\pywebview``: that directory is
    shared with other pywebview applications and a half-written one makes
    WebView2 initialization fail, which pywebview only logs.  A marker left by
    the previous failure makes the next start recreate the profile.
    """
    root = os.environ["DISKWARDEN_DATA_ROOT"]
    profile = os.path.join(root, WEBVIEW_DIR)
    marker = os.path.join(root, WEBVIEW_FAILED_MARKER)

    if os.path.exists(marker):
        sys.stderr.write("previous WebView2 start failed; resetting %s\n" % profile)
        shutil.rmtree(profile, ignore_errors=True)
        try:
            os.remove(marker)
        except OSError:
            pass

    try:
        os.makedirs(profile)
    except OSError:
        pass
    return profile, marker


def _install_webview(profile, marker):
    # type: (str, str) -> bool
    """Point pywebview at our profile and make WebView2 failures visible."""
    try:
        import webview
        from webview.platforms import edgechromium, winforms
    except Exception:
        _message("Disk Warden 无法加载 WebView 组件：\n\n" + traceback.format_exc())
        return False

    # MSHTML cannot render ui/index.html (the SPA needs a modern engine), so a
    # forced fallback or a missing WebView2 runtime must not reach the window.
    forced = (os.environ.get("PYWEBVIEW_GUI") or "").strip().lower()
    if forced in ("mshtml", "cef") or not winforms.is_chromium:
        _message(
            "Disk Warden 需要 Microsoft Edge WebView2 运行时，但当前系统未检测到"
            "可用的 WebView2。\n\n"
            "请安装 Microsoft Edge WebView2 Runtime（免费）后重新启动：\n"
            "https://developer.microsoft.com/microsoft-edge/webview2/\n\n"
            "Windows 10/11 通常已随 Edge 安装；若没有，可下载 Evergreen "
            "Standalone Installer。"
        )
        return False

    original_start = webview.start

    def start(*args, **kwargs):
        kwargs.setdefault("storage_path", profile)
        return original_start(*args, **kwargs)

    webview.start = start

    original_ready = edgechromium.EdgeChrome.on_webview_ready

    def on_webview_ready(self, sender, args):
        if not args.IsSuccess:
            detail = str(args.InitializationException)
            sys.stderr.write("WebView2 initialization failed: %s\n" % detail)
            try:
                with open(marker, "w", encoding="utf-8") as handle:
                    handle.write(detail + "\n")
            except OSError:
                pass
            _message(
                "Disk Warden 无法初始化 WebView2，界面无法显示。\n\n%s\n\n"
                "请关闭本窗口后重新打开 Disk Warden，程序会自动重建 WebView 缓存；"
                "如果仍然失败，请安装或修复 Microsoft Edge WebView2 Runtime。"
                % detail
            )
            # Closing the form from inside the WebView2 callback re-enters
            # WinForms shutdown and can hang, so end the process instead.
            _fatal_exit(3)
            return
        return original_ready(self, sender, args)

    edgechromium.EdgeChrome.on_webview_ready = on_webview_ready
    return True


def _run():
    # type: () -> int
    from src.app.main import main

    return main()


if __name__ == "__main__":
    if not _check_runtime_layout():
        sys.exit(3)
    if not _prepare_data_root():
        sys.exit(1)
    _redirect_logs(os.environ["DISKWARDEN_DATA_ROOT"])
    _warn_low_integrity()
    profile, marker = _prepare_webview_profile()
    if not _install_webview(profile, marker):
        sys.exit(2)
    try:
        code = _run()
    except Exception:
        _message("Disk Warden 启动失败：\n\n" + traceback.format_exc())
        code = 1
    sys.exit(code)
