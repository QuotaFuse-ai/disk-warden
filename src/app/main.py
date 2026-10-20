# -*- coding: utf-8 -*-
"""Desktop launcher: pywebview hosts ``ui/index.html``, Bridge hosts the UI.

Run with ``python main.py`` (or ``python -m src.app.main``).  ``--tray`` starts
with the window hidden and only the tray icon visible; it is also the argument
written to the per-user startup item by the settings switch.
"""

import logging
import os
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

APP_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(APP_DIR))
UI_PATH = os.path.join(REPO_ROOT, "ui", "index.html")
ICON_PATH = os.path.join(REPO_ROOT, "packaging", "DiskWarden.ico")

TRAY_ARGS = ("--tray", "--hidden")
SELFTEST_ENV = "DISKWARDEN_TRAY_SELFTEST_MS"

LOGGER = logging.getLogger("disk_warden.app.main")


def _configure_logging(logs_dir=None, debug=False):
    # type: (object, bool) -> None
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if logs_dir:
        try:
            if not os.path.isdir(logs_dir):
                os.makedirs(logs_dir)
            handler = RotatingFileHandler(
                os.path.join(logs_dir, "app.log"),
                maxBytes=2 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
            handler.setFormatter(formatter)
            root.addHandler(handler)
        except OSError:
            pass
    if debug:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)


def create_window(service, bridge, debug=False, hidden=False):
    # type: (object, object, bool, bool) -> object
    import webview  # imported lazily so tests do not need a GUI toolkit

    if not os.path.isfile(UI_PATH):
        raise IOError("UI file not found: %s" % UI_PATH)

    return webview.create_window(
        "Disk Warden",
        UI_PATH,
        js_api=bridge,
        width=1280,
        height=840,
        min_size=(1024, 700),
        background_color="#F5F6F8",
        text_select=True,
        hidden=hidden,
    )


def wants_tray(argv):
    # type: (object) -> bool
    return any(arg in TRAY_ARGS for arg in (argv or ()))


def _selftest_sequence(window, tray, quit_app, delay_ms):
    # type: (object, object, object, int) -> None
    """Drive show/hide/quit from the tray path for automated release checks.

    The packaged window has no console, so the sequence is written to
    ``app.log`` and the caller (the QA script) observes the window visibility
    and the process exit instead of reading stdout.  It is opt-in: nothing
    schedules it unless ``DISKWARDEN_TRAY_SELFTEST_MS`` is set.
    """

    def sequence():
        steps = (
            ("show", lambda: tray.show_window(), 1.0),
            ("hide", lambda: tray.hide_window(notify=False), 1.0),
            ("quit", quit_app, 0.0),
        )
        time.sleep(max(0, delay_ms) / 1000.0)
        for name, action, pause in steps:
            LOGGER.info("tray selftest step: %s", name)
            try:
                action()
            except Exception as exc:  # pragma: no cover - QA diagnostics
                LOGGER.error("tray selftest step %s failed: %s", name, exc)
            if pause:
                time.sleep(pause)

    threading.Thread(target=sequence, name="diskwarden-tray-selftest", daemon=True).start()


def main(argv=None):
    # type: (object) -> int
    argv = list(sys.argv[1:] if argv is None else argv)
    debug = os.environ.get("DISK_WARDEN_DEBUG", "") == "1"
    os.environ.setdefault("PYTHONUTF8", "1")

    from . import APP_VERSION
    from .service import create_service

    service = create_service()
    _configure_logging(service.layout.logs_dir, debug=debug)
    LOGGER.info("starting disk-warden %s (data root %s)", APP_VERSION, service.layout.root)

    try:
        import webview
    except ImportError as exc:
        LOGGER.error("pywebview is not installed: %s", exc)
        sys.stderr.write(
            "pywebview is required to run the desktop UI.\n"
            "Install it with: python -m pip install -r requirements.txt\n"
        )
        service.close()
        return 1

    from .bridge import Bridge
    from .tray import TrayController

    bridge = Bridge(service)
    start_hidden = wants_tray(argv)
    window = create_window(service, bridge, debug=debug, hidden=start_hidden)

    settings = service.settings_get()
    language = settings.get("language") or "zh-CN"
    quitting = {"value": False}
    tray = TrayController(
        window,
        logs_dir=service.layout.logs_dir,
        language=language,
        icon_path=ICON_PATH,
    )
    if tray.start():
        LOGGER.info("tray icon started (window hidden=%s)", start_hidden)
    else:
        LOGGER.warning("tray icon could not start; the window closes normally")

    def quit_app():
        # type: () -> None
        if quitting["value"]:
            return
        quitting["value"] = True
        LOGGER.info("quit requested")
        tray.stop()
        try:
            window.destroy()
        except Exception as exc:  # pragma: no cover - window already gone
            LOGGER.warning("could not destroy the window: %s", exc)

    tray.on_quit = quit_app

    def on_closing():
        # type: () -> bool
        """Close button hides to the tray; quitting happens from the menu."""
        if quitting["value"] or not tray.running:
            return True
        LOGGER.info("window close requested; hiding to tray")
        tray.hide_window()
        return False

    window.events.closing += on_closing

    selftest_ms = os.environ.get(SELFTEST_ENV, "").strip()
    if selftest_ms.isdigit():
        _selftest_sequence(window, tray, quit_app, int(selftest_ms))

    try:
        webview.start(debug=debug, private_mode=False)
    finally:
        tray.stop()
        service.close()
        LOGGER.info("disk-warden stopped")
    del window
    return 0


if __name__ == "__main__":
    sys.exit(main())
