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
"""

import ctypes
import os
import sys
import tempfile
import traceback

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)

from src.core.store.layout import DataLayout, default_data_root  # noqa: E402


def _message(text):
    # type: (str) -> None
    try:
        ctypes.windll.user32.MessageBoxW(None, text, "Disk Warden", 0x10)
    except Exception:
        sys.stderr.write(text + "\n")


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


def _run():
    # type: () -> int
    from src.app.main import main

    return main()


if __name__ == "__main__":
    if not _prepare_data_root():
        sys.exit(1)
    try:
        code = _run()
    except Exception:
        _message("Disk Warden 启动失败：\n\n" + traceback.format_exc())
        code = 1
    sys.exit(code)
