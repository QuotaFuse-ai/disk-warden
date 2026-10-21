# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the unsigned macOS ``DiskWarden.app``.

Build from the repository root on macOS:

    python packaging/make_icns.py
    python -m PyInstaller --noconfirm --clean packaging/DiskWarden-macos.spec

``packaging/build-macos.sh`` runs both steps plus the ``--smoke`` check and the
zip; CI (``.github/workflows/build-macos.yml``) does the same.

Differences from the Windows spec, all packaging-only:

* ``BUNDLE`` wraps the onedir output in a ``.app``; the bundle identifier is
  the same one the LaunchAgent plist uses (``com.quotafuse.diskwarden``).
* ``webview.platforms.cocoa`` replaces the EdgeChromium/WebView2 backends, and
  ``pystray._darwin`` replaces the Win32 tray backend.
* ``pythonnet``/``clr_loader`` are excluded: they are Windows-only and the
  macOS requirements file does not install them.
* ``argv_emulation`` stays off.  Apple-event rewriting would swallow the
  ``--tray``/``--hidden`` arguments the startup item passes.
* No ``version=``/``DiskWarden.exe.config``: both are Win32 resources.

``ui/index.html`` and the two JSON data files sit at the application root so
``src/app/main.py``'s ``REPO_ROOT`` lookup keeps working frozen.  No business
logic is modified for packaging.
"""

import os
import re

ROOT = os.path.dirname(SPECPATH)


def _app_version():
    # type: () -> str
    path = os.path.join(ROOT, "src", "app", "__init__.py")
    try:
        with open(path, "r", encoding="utf-8") as stream:
            match = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', stream.read(), re.M)
    except OSError:
        return ""
    return match.group(1) if match else ""


APP_VERSION = _app_version()
_numeric = re.match(r"(\d+\.\d+\.\d+)", APP_VERSION)
SHORT_VERSION = _numeric.group(1) if _numeric else "0.0.0"
_beta = re.search(r"beta\.(\d+)$", APP_VERSION)
BUILD_NUMBER = _beta.group(1) if _beta else "1"

ICNS = os.path.join(ROOT, "packaging", "DiskWarden.icns")
if not os.path.isfile(ICNS):
    # Generated from the committed .ico; a missing icon must not fail the
    # build, but shipping the PyInstaller default icon silently would hide a
    # forgotten step.
    print("WARNING: %s is missing; run packaging/make_icns.py first" % ICNS)

a = Analysis(
    [os.path.join(ROOT, "packaging", "launcher.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[
        (os.path.join(ROOT, "ui", "index.html"), "ui"),
        (os.path.join(ROOT, "ui", "assets"), "ui/assets"),
        (os.path.join(ROOT, "data", "ide-catalog.json"), "data"),
        (os.path.join(ROOT, "data", "rules.json"), "data"),
        # src/app/main.py resolves ICON_PATH under REPO_ROOT/packaging once
        # frozen; Pillow reads the .ico for the menu-bar image.
        (os.path.join(ROOT, "packaging", "DiskWarden.ico"), "packaging"),
    ],
    hiddenimports=[
        "psutil",
        "pystray",
        "pystray._darwin",
        "PIL.Image",
        "PIL.ImageDraw",
        "webview.platforms.cocoa",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "pytest",
        "tests",
        "pythonnet",
        "clr_loader",
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

app = BUNDLE(
    coll,
    name="DiskWarden.app",
    icon=ICNS if os.path.isfile(ICNS) else None,
    bundle_identifier="com.quotafuse.diskwarden",
    info_plist={
        "CFBundleName": "Disk Warden",
        "CFBundleDisplayName": "Disk Warden",
        "CFBundleIdentifier": "com.quotafuse.diskwarden",
        "CFBundleExecutable": "DiskWarden",
        "CFBundleShortVersionString": SHORT_VERSION,
        "CFBundleVersion": BUILD_NUMBER,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundlePackageType": "APPL",
        "CFBundleSignature": "????",
        "LSApplicationCategoryType": "public.app-category.utilities",
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "Copyright (c) QuotaFuse",
    },
)
