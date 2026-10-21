# Security

Disk Warden moves real data, so trust matters. This page covers what we ship, how to verify it, and how the repository is checked.

## Installing without cloning

You do not need the source tree to run Disk Warden. Every release publishes a complete archive:

- `DiskWarden-<version>-win-x64.zip` for Windows 10 / 11 x64
- `DiskWarden-<version>-macos-arm64.zip` for macOS 11+ on Apple Silicon (currently **untested on real hardware**)

Both archives are built from a tagged commit by our own build pipeline (Windows: `packaging/build.ps1`; macOS: GitHub Actions `macos-latest`). Neither is code-signed yet, so Gatekeeper or SmartScreen will warn the first time — see the first-launch steps in the README.

On Windows, unpack the archive into a fixed folder **on the C: drive** (`C:\DiskWarden` is the recommended path) and run it from there. Running from inside the zip preview, a temporary folder, a USB stick or a network share is not supported: the location is read-only, disappears, or changes path, and the junctions created during a migration would break.

## Verify what you downloaded

Each archive ships next to a `.sha256` file on the Releases page.

```powershell
# Windows PowerShell
Get-FileHash .\DiskWarden-0.9.0-beta.10-win-x64.zip -Algorithm SHA256
```

```bash
# macOS / Linux
shasum -a 256 DiskWarden-0.9.0-beta.10-macos-arm64.zip
```

Compare the output with the `.sha256` asset for the same release. If it differs, do not run the archive.

## What the app touches

- It reads the data directories of AI IDEs it recognises; no other user files are opened.
- It writes migrated data, a journal, history, settings and logs under its own data root, never inside the app folder: `D:\DiskWarden` on Windows when that drive exists and is writable, otherwise `%LOCALAPPDATA%\DiskWarden`; `~/Library/Application Support/DiskWarden` on macOS. The Settings page shows the path in use.
- It creates junctions (Windows) or symlinks (macOS) only at the directories you approve.
- Every move is copied, verified, and only then linked; the original stays in place until verification passes.
- Network access is limited to GitHub (sign-in and the star check) and, if you explicitly configure it, one AI endpoint you supply. AI analysis is off by default.

## Repository safety check

`tools/repo_safety_check.py` scans the tracked tree offline — it executes nothing and uploads nothing:

```bash
python tools/repo_safety_check.py                       # summary, exit 1 on any high finding
python tools/repo_safety_check.py --md report.md        # Markdown report
python tools/repo_safety_check.py --zip <archive.zip>   # also inspect a release archive
```

It flags:

- compiled executables or installers tracked in git
- committed credentials (private keys, GitHub / AWS / OpenAI / Slack tokens, hard-coded literals)
- download-and-run and obfuscated payloads in scripts (`curl | sh`, `-EncodedCommand`)
- destructive deletes aimed at root or home paths, shell-backed subprocesses, dynamic `eval`/`exec`
- outbound hosts outside a documented allowlist
- symlinks, oversized files, disguised double extensions, archive path traversal

The latest result is committed in [`docs/security/repo-safety-report.md`](docs/security/repo-safety-report.md). The repository tracks **no compiled binaries**: every executable users receive is produced by the build pipeline and attached to a release, and is listed with its SHA-256 in the report.

## Reporting a vulnerability

Open a private security advisory on the repository, or email the maintainers listed on the GitHub organisation page. Please do not file a public issue for an exploitable problem. We aim to acknowledge reports within 72 hours.
