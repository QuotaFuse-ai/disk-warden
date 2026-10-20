# -*- coding: utf-8 -*-
"""System tray icon: show / hide / open logs / quit (T15).

pystray owns its own message loop on Windows, so the icon runs in a daemon
thread while pywebview keeps the main thread.  Every callback that touches the
window goes through pywebview's thread-marshalled ``show``/``hide``/``destroy``
methods, which is what makes the tray usable while the form is hidden.

The controller deliberately knows nothing about the service: ``main`` passes
the window, the log directory and a quit callback, so the tray can be unit
tested with a fake window and a fake icon.
"""

import os
import threading

_LABELS = {
    "zh": {
        "show": "显示主窗口",
        "hide": "隐藏到托盘",
        "logs": "打开日志目录",
        "quit": "退出 Disk Warden",
        "title": "Disk Warden",
        "running": "Disk Warden 仍在托盘运行，右键托盘图标可显示窗口或退出。",
    },
    "en": {
        "show": "Show Disk Warden",
        "hide": "Hide to tray",
        "logs": "Open log folder",
        "quit": "Quit Disk Warden",
        "title": "Disk Warden",
        "running": "Disk Warden is still running in the tray. Right-click the icon to show or quit.",
    },
}


def labels_for(language):
    # type: (str) -> dict
    return _LABELS["zh"] if str(language or "").lower().startswith("zh") else _LABELS["en"]


def _load_image(icon_path):
    # type: (str) -> object
    from PIL import Image, ImageDraw

    if icon_path and os.path.isfile(icon_path):
        try:
            return Image.open(icon_path).convert("RGBA")
        except Exception:
            pass
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((2, 2, 62, 62), radius=14, fill=(37, 99, 235, 255))
    draw.text((22, 18), "D", fill=(255, 255, 255, 255))
    return image


class TrayController(object):
    """Owns one pystray icon and maps its menu to window actions."""

    def __init__(
        self,
        window,
        logs_dir="",
        language="zh",
        icon_path="",
        on_quit=None,
        icon_factory=None,
    ):
        # type: (object, str, str, str, object, object) -> None
        self.window = window
        self.logs_dir = logs_dir or ""
        self.language = language
        self.icon_path = icon_path
        self.on_quit = on_quit
        self._icon_factory = icon_factory
        self._icon = None
        self._thread = None
        self._quitting = False
        self._warned = False
        self._lock = threading.Lock()

    # -- lifecycle -------------------------------------------------------

    @property
    def running(self):
        # type: () -> bool
        return self._icon is not None

    def start(self):
        # type: () -> bool
        """Create and show the icon; returns False when pystray is unusable."""
        if self._icon is not None:
            return True
        try:
            icon = self._build_icon()
        except Exception:
            return False
        self._icon = icon
        self._thread = threading.Thread(
            target=self._run_icon, name="diskwarden-tray", daemon=True
        )
        self._thread.start()
        return True

    def _run_icon(self):
        try:
            self._icon.run()
        except Exception:
            # A tray failure must never take the app down; the window stays
            # usable and the user can still close it normally.
            self._icon = None

    def stop(self):
        # type: () -> None
        icon = self._icon
        self._icon = None
        if icon is None:
            return
        try:
            icon.stop()
        except Exception:
            pass

    def _build_icon(self):
        # type: () -> object
        labels = labels_for(self.language)
        if self._icon_factory is not None:
            return self._icon_factory(labels, _MenuItem)

        import pystray

        menu = pystray.Menu(
            pystray.MenuItem(labels["show"], self.show_window, default=True),
            pystray.MenuItem(labels["hide"], self.hide_window),
            pystray.MenuItem(labels["logs"], self.open_logs),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(labels["quit"], self.quit),
        )
        return pystray.Icon(
            "DiskWarden", _load_image(self.icon_path), labels["title"], menu
        )

    # -- menu actions ----------------------------------------------------

    def _safe(self, action):
        # type: (object) -> object
        try:
            return action()
        except Exception:
            return None

    def show_window(self):
        # type: () -> None
        self._safe(lambda: self.window.show())
        self._safe(lambda: self.window.restore())

    def hide_window(self, notify=True):
        # type: (bool) -> None
        self._safe(lambda: self.window.hide())
        if notify and not self._warned:
            self._warned = True
            self.notify(labels_for(self.language)["title"], labels_for(self.language)["running"])

    def open_logs(self):
        # type: () -> None
        if not self.logs_dir:
            return
        try:
            from src.platform import reveal_directory

            reveal_directory(self.logs_dir)
        except Exception:
            pass

    def quit(self):
        # type: () -> None
        with self._lock:
            if self._quitting:
                return
            self._quitting = True
        self.stop()
        if self.on_quit is not None:
            self._safe(self.on_quit)
        else:
            self._safe(lambda: self.window.destroy())

    def notify(self, title, message):
        # type: (str, str) -> None
        icon = self._icon
        if icon is None:
            return
        try:
            icon.notify(message, title)
        except Exception:
            pass


class _MenuItem(object):
    """Minimal stand-in for ``pystray.MenuItem`` used by the tests."""

    def __init__(self, text, action, default=False):
        # type: (str, object, bool) -> None
        self.text = text
        self.action = action
        self.default = default


__all__ = ["TrayController", "labels_for"]
