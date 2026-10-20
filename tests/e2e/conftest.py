# -*- coding: utf-8 -*-
"""Keep the end-to-end suite cheap and sandboxed when pytest collects it.

CI runs ``python -m pytest tests -q``, which reaches this package.  Without a
pin, ``support.choose_scale()`` would pick the multi-gigabyte ``real`` profile
whenever the runner's base drive has 9 GiB free, turning a normal unit-test run
into a ~3 minute disk drill (and filling the runner's disk).

So under pytest:

* the scale defaults to ``small`` unless ``DISKWARDEN_E2E_SCALE`` says
  otherwise;
* the sandbox base defaults to a directory inside the system temp dir, so no
  sandbox is ever created at a drive root.

Explicit environment overrides always win, and ``tools/run_e2e.py`` does not go
through pytest, so ``python tools/run_e2e.py`` keeps auto-selecting ``real``.
"""

import os
import tempfile

if not (os.environ.get("DISKWARDEN_E2E_SCALE") or "").strip():
    os.environ["DISKWARDEN_E2E_SCALE"] = "small"

if not (os.environ.get("DISKWARDEN_E2E_ROOT") or "").strip():
    base = os.path.join(tempfile.gettempdir(), "disk-warden-e2e")
    if not os.path.isdir(base):
        os.makedirs(base, exist_ok=True)
    os.environ["DISKWARDEN_E2E_ROOT"] = base
