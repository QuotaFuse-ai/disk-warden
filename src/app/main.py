# -*- coding: utf-8 -*-
"""Desktop launcher: pywebview hosts ``ui/index.html``, Bridge hosts the UI.

Run with ``python main.py`` (or ``python -m src.app.main``).
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

APP_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(APP_DIR))
UI_PATH = os.path.join(REPO_ROOT, "ui", "index.html")

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


def create_window(service, bridge, debug=False):
    # type: (object, object, bool) -> object
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
    )


def main(argv=None):
    # type: (object) -> int
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

    bridge = Bridge(service)
    window = create_window(service, bridge, debug=debug)
    try:
        webview.start(debug=debug, private_mode=False)
    finally:
        service.close()
        LOGGER.info("disk-warden stopped")
    del window
    return 0


if __name__ == "__main__":
    sys.exit(main())
