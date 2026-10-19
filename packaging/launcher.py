# -*- coding: utf-8 -*-
"""PyInstaller entry point.

The frozen application cannot use ``python -m src.app.main``, so this launcher
puts the repository root (``sys._MEIPASS`` when frozen) on ``sys.path`` and
delegates to the real host entry point.  It lives under ``packaging/`` on
purpose: no business code is duplicated here.
"""

import os
import sys

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)

from src.app.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
