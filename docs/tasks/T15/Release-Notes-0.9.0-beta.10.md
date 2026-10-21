# Disk Warden 0.9.0-beta.10 release notes

- Release date: 2026-10-21
- Version: `0.9.0-beta.10` (source: `main` at `5a4bb81`)
- Release page: <https://github.com/QuotaFuse-ai/disk-warden/releases/tag/v0.9.0-beta.10>

| Platform | Artifact | Bytes | SHA-256 |
|---|---|---|---|
| Windows 10/11 x64 | `DiskWarden-0.9.0-beta.10-win-x64.zip` | 36,700,380 | `6182d6d433ecead2c77b5c11925d932d5f2942ef6fad416bebd2c75d3ef9f9dd` |
| macOS 11+ arm64 | `DiskWarden-0.9.0-beta.10-macos-arm64.zip` | 16,498,327 | `0659f0cf4193c12063f5c0bda877604afdd211e2ad79528c810119a5711ef867` |

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
| Unit tests (Windows, `python -m unittest discover -s tests`) | 934 run, OK (1 skipped) |
| CI tests (Linux 3.10/3.12, PR #16) | pass |
| Packaged sandbox drills (`packaging/run-drills.ps1`) | 8/8 pass |
| macOS CI: unit tests, packaging, `--smoke` | pass (`ok: true`, `app_version = 0.9.0-beta.10`) |
| `tools/repo_safety_check.py` on the tree and both archives | 217 files, 0 high, verdict PASS |
| Install note present in bundled `START-HERE.txt` and docs | yes, both archives (205 / 203 entries) |

## Known limitations (unchanged)

- macOS automatic scan returns zero apps: the IDE catalog and `src/core/scan/discovery.py` are
  Windows-only.
- macOS sign-in is not persisted (no DPAPI equivalent).
- Both builds unsigned: one SmartScreen / Gatekeeper prompt on first launch.
