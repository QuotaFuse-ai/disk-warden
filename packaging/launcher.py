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
    add(os.path.join(tempfile.gettempdir(), "DiskWarden"))
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
    if not _prepare_data_root():
        sys.exit(1)
    _redirect_logs(os.environ["DISKWARDEN_DATA_ROOT"])
    profile, marker = _prepare_webview_profile()
    if not _install_webview(profile, marker):
        sys.exit(2)
    try:
        code = _run()
    except Exception:
        _message("Disk Warden 启动失败：\n\n" + traceback.format_exc())
        code = 1
    sys.exit(code)
