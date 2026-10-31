#!/bin/bash
# Build the unsigned macOS app locally, the same way CI does.
#
#   ./packaging/build-macos.sh
#
# The result is dist/DiskWarden.app plus a zip that keeps the bundle intact.
# This is the script a human runs when they have a Mac and want to do the
# click-through verification that CI cannot: run it, unzip the archive, then
# follow the first-launch steps in START-HERE-macOS.txt.
#
# Nothing here needs an Apple account: no signing and no notarization, so
# Gatekeeper will ask for approval on first launch.  That is expected.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"

if [ "$(uname -s)" != "Darwin" ]; then
    echo "error: this script builds a .app and only runs on macOS." >&2
    exit 1
fi

if [ ! -x "$PYTHON" ] && ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "error: $PYTHON not found; install Python 3.12 or set PYTHON=..." >&2
    exit 1
fi

"$PYTHON" -c 'import PyInstaller, webview, psutil, pystray, PIL' 2>/dev/null || {
    echo "error: build dependencies are missing. Run:" >&2
    echo "  $PYTHON -m pip install -r packaging/requirements-build-macos.txt" >&2
    exit 1
}

echo "==> icon"
"$PYTHON" packaging/make_icns.py

echo "==> DiskWarden.app"
"$PYTHON" -m PyInstaller --noconfirm --clean \
    --distpath dist --workpath build \
    packaging/DiskWarden-macos.spec

APP="dist/DiskWarden.app"
BUNDLE_ID="$(plutil -extract CFBundleIdentifier raw "$APP/Contents/Info.plist")"
if [ "$BUNDLE_ID" != "com.quotafuse.diskwarden" ]; then
    echo "error: unexpected bundle identifier: $BUNDLE_ID" >&2
    exit 1
fi

echo "==> smoke test"
"$APP/Contents/MacOS/DiskWarden" --smoke --smoke-out "$PWD/smoke.json"
"$PYTHON" - <<'PY'
import json

report = json.load(open("smoke.json"))
assert report["ok"], report
assert report["webview_backend"] == "cocoa", report
assert report["ui_present"], report
assert report["frozen"], report
print("smoke ok:", report["data_root"])
PY

echo "==> zip"
VERSION="$("$PYTHON" -c 'from src.app import APP_VERSION; print(APP_VERSION)')"
ZIP="dist/DiskWarden-$VERSION-macos-$(uname -m).zip"
# Zip root holds the bundle and START-HERE-macOS.txt: macOS blocks the first
# launch before any window exists, so the file must be readable beforehand.
STAGE="dist/stage/DiskWarden-$VERSION-macos-$(uname -m)"
mkdir -p "$STAGE"
# ditto keeps symlinks, permissions and extended attributes; zip(1) does not.
ditto "$APP" "$STAGE/DiskWarden.app"
cp packaging/START-HERE-macOS.txt "$STAGE/"
ditto -c -k --keepParent "$STAGE" "$ZIP"
unzip -l "$ZIP" | grep -q "START-HERE-macOS.txt" || {
  echo "START-HERE-macOS.txt is missing from the zip" >&2; exit 1; }
shasum -a 256 "$ZIP" | tee "$ZIP.sha256"

cat <<EOF

Done: $ZIP

To verify by hand (CI cannot click through):
  1. unzip the archive somewhere new, e.g. ~/Desktop
  2. double-click DiskWarden.app
  3. macOS blocks the unsigned app: follow packaging/START-HERE-macOS.txt
  4. scan, migrate, then roll the migration back once; confirm no data loss

The app writes its data to ~/Library/Application Support/DiskWarden and its
logs to logs/app.log below that folder.
EOF
