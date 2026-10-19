# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir spec for the unsigned Windows development build.

Build from the repository root:

    python -m PyInstaller --noconfirm --clean packaging/DiskWarden.spec

The launcher lives in ``packaging/launcher.py``; ``ui/index.html`` and the two
JSON data files are bundled at the application root so that the existing
``REPO_ROOT``-based resource lookup works frozen as well.  No business logic
is modified for packaging.
"""

import os

ROOT = os.path.dirname(SPECPATH)

a = Analysis(
    [os.path.join(ROOT, "packaging", "launcher.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[
        (os.path.join(ROOT, "ui", "index.html"), "ui"),
        (os.path.join(ROOT, "data", "ide-catalog.json"), "data"),
        (os.path.join(ROOT, "data", "rules.json"), "data"),
        (os.path.join(ROOT, "packaging", "DiskWarden.exe.config"), "."),
    ],
    hiddenimports=[
        "psutil",
        "webview.platforms.edgechromium",
        "webview.platforms.winforms",
        "webview.platforms.mshtml",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "pytest",
        "tests",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DiskWarden",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=os.path.join(ROOT, "packaging", "version_info.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="DiskWarden",
)
