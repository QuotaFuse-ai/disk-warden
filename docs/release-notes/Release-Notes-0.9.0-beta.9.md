# Disk Warden 0.9.0-beta.9 release notes

- Release date: 2026-10-21
- Version: `0.9.0-beta.9` (source: `main` at `53ccedc`)
- Release page: <https://github.com/QuotaFuse-ai/disk-warden/releases/tag/v0.9.0-beta.9>

| Platform | Artifact | Bytes | SHA-256 |
|---|---|---|---|
| Windows 10/11 x64 | `DiskWarden-0.9.0-beta.9-win-x64.zip` | 36,699,422 | `a44d93fd4af5799fff6d49938398c384da774e100eee8c8ec2e75d67755fbf09` |
| macOS 11+ arm64 | `DiskWarden-0.9.0-beta.9-macos-arm64.zip` | 16,499,307 | `1f733f952d9469cd1627830d1261efa3b7cf9ac37f728adf6e0d5168acfee111` |

Both builds are unsigned. The macOS build is **not tested on real hardware** (built by
GitHub Actions `macos-latest`, automated `--smoke` check only).

## What changed

- **English is the default UI language.** `ui/index.html` ships `let LANG = 'en'` and
  `<html lang="en">`; `localStorage['dw-lang']` and `?lang=` still override it, and the title-bar
  `中文` / `EN` chip switches to Simplified Chinese and remembers the choice. The tray labels
  default to English (`src/app/main.py`).
- **No Chinese leaks into an English session.** The engine returns Chinese `state_label` and
  `blocked_reason`; the UI now translates from `state` and `blocked_code` (`state.*`,
  `blockedCode.*` keys) and no longer falls back to `I18N['zh-CN']` anywhere.
- **GitHub-facing docs are English-first**: `README.md`, the release page, this file and
  `SECURITY.md`; `README.zh-CN.md` remains the opt-in Chinese version.
- **Repository safety check (new).** `tools/repo_safety_check.py` audits the tracked tree offline
  and read-only: committed binaries, secret patterns and private keys, pipe-to-shell and
  obfuscated payloads, destructive deletes, unlisted outbound hosts, symlinks, oversized and
  disguised files, plus path traversal inside release archives. It runs in
  `.github/workflows/tests.yml`.
- **`SECURITY.md`**: installing without cloning, download verification, what the app touches on
  disk and on the network, vulnerability reporting.
- **Fix**: `AuthManager._complete` published the login before marking the device flow
  authenticated, so a poller could read `signed_in: true` with `flow.state == "pending"`.

## Verification performed

| Check | Result |
|---|---|
| Unit tests (Windows, `python -m unittest discover -s tests`) | 934 run, OK (1 skipped) |
| CI tests (Linux 3.10/3.12) | pass |
| Packaged sandbox drills (`packaging/run-drills.ps1`) | 8/8 pass |
| Windows frozen self-check / tray self-test | pass |
| macOS CI: unit tests, packaging, `--smoke` | pass (`ok: true`, `app_version = 0.9.0-beta.9`) |
| `tools/repo_safety_check.py` on the tree and both archives | 0 high, verdict PASS |
| Bundled `ui/index.html` in both artifacts | `let LANG = 'en';`, `<html lang="en">` |

## Known limitations (unchanged)

- macOS automatic scan returns zero apps: the IDE catalog and `src/core/scan/discovery.py` are
  Windows-only.
- macOS sign-in is not persisted (no DPAPI equivalent).
- Both builds unsigned: one SmartScreen / Gatekeeper prompt on first launch.
