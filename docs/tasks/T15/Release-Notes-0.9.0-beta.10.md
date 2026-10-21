# Disk Warden 0.9.0-beta.10 release notes

- Release date: 2026-10-21
- Version: `0.9.0-beta.10` (source: `main` at `TBD`)
- Release page: <https://github.com/QuotaFuse-ai/disk-warden/releases/tag/v0.9.0-beta.10>

| Platform | Artifact | Bytes | SHA-256 |
|---|---|---|---|
| Windows 10/11 x64 | `DiskWarden-0.9.0-beta.10-win-x64.zip` | TBD | `TBD` |
| macOS 11+ arm64 | `DiskWarden-0.9.0-beta.10-macos-arm64.zip` | TBD | `TBD` |

Both builds are unsigned. The macOS build is **not tested on real hardware** (built by
GitHub Actions `macos-latest`, automated `--smoke` check only).

## What changed

- **The Windows install location is documented as the C: drive.** Users are told to unpack the
  archive into a fixed folder on the system drive (`C:\DiskWarden` recommended) and run it from
  there, and why: running from the zip preview, a temporary folder, a USB stick or a network
  share leaves the app on a read-only, vanishing or changing path, and the junctions created
  during a migration would break. Covered in `README.md`, `README.zh-CN.md`, `SECURITY.md`,
  `packaging/START-HERE.txt` (inside the zip) and the release page.
- **Data root documented accurately.** `SECURITY.md` claimed `%LOCALAPPDATA%\DiskWarden` on
  Windows; the launcher actually uses `D:\DiskWarden` when that drive exists and is writable and
  only then falls back to `%LOCALAPPDATA%\DiskWarden`. The docs now match the launcher, and
  `START-HERE.txt` explains how to pin the data root to the C: drive with
  `DISKWARDEN_DATA_ROOT`.
- Version bump to `0.9.0-beta.10` in the app, packaged UI, installer metadata and docs.

No application logic changed in this release.

## Verification performed

| Check | Result |
|---|---|
| Unit tests (Windows, `python -m unittest discover -s tests`) | TBD |
| CI tests (Linux 3.10/3.12) | TBD |
| Packaged sandbox drills (`packaging/run-drills.ps1`) | TBD |
| macOS CI: unit tests, packaging, `--smoke` | TBD |
| `tools/repo_safety_check.py` on the tree and both archives | TBD |
| Install note present in bundled `START-HERE.txt` and docs | TBD |

## Known limitations (unchanged)

- macOS automatic scan returns zero apps: the IDE catalog and `src/core/scan/discovery.py` are
  Windows-only.
- macOS sign-in is not persisted (no DPAPI equivalent).
- Both builds unsigned: one SmartScreen / Gatekeeper prompt on first launch.
