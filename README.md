# Disk Warden

English | [Chinese](README.zh-CN.md)

**Disk Warden** is a storage manager for AI coding tools. It finds the data directories of Cursor, Trae, Claude Code and other AI IDEs on your machine, moves the reproducible bulk (caches, indexes, logs) to another drive, and keeps the original paths working — so your system drive stops filling up.

Runs on **Windows and macOS**.

## Why not just a cleanup tool

- It understands AI IDE directory layouts and classifies each directory as **movable**, **do-not-touch**, or **unknown** — instead of deleting files by extension
- After a move, the original path becomes a link (junction on Windows, symlink on macOS); IDEs and shells keep working unchanged
- Every move is backed up first and verified afterwards; any failure rolls back, and the source data is never the only copy at risk
- Migrated directories can be rolled back with one click; interrupted migrations are recovered automatically on next start
- Optional online AI analysis for unknown directories only — **off by default**

## Status

| Capability | Status |
|---|---|
| Directory scan, size accounting, risk classification (T06–T08) | Done |
| Copy, verification, link creation, crash recovery (T09–T10) | Done |
| Rollback engine and local store: journal / history / settings / learned rules (T11) | Done, merged to `main` |
| AI analysis interface (T22) | Done, disabled by default |
| UI wired to the real engine (T12) | In progress |
| Windows / macOS packaging (T15 / T21) | Not started |

**437 unit tests** cover the completed modules.

## Platforms

- **Windows 10/11** — primary development and verification platform. The core engine is implemented: robocopy transfer, `mklink /J` links, process-in-use detection, volume and free-space checks.
- **macOS 12+** — in scope for the first release (decision 0004). The platform adapter layer is split by interface, with symlink and process-detection scaffolding in place; full adaptation, Gatekeeper first-launch guidance and `.app` packaging land in T21, built on GitHub Actions `macos-latest` (decision 0007).

## Stack

- Python 3.8+ as the code baseline; 3.12 for official builds (decision 0005)
- UI: a single self-contained HTML/JS page (`ui/index.html`) hosted by pywebview (WebView2 on Windows, WKWebView on macOS)
- Packaging: PyInstaller onedir; macOS artifacts built in CI

## Development

Requires Python 3.8 or newer.

```bash
# Full unit test suite
python -m unittest discover -s tests -q

# Migration drills: sandboxed in the system temp directory, never touch real user data
python tools/migrate_drill.py
python tools/migrate_drill_b.py

# View the UI prototype: static page with sample data
# Open ui/index.html in a browser
```

The desktop app itself (real data + native shell) becomes runnable after T12 and T15. `main.py` is a deprecated Tkinter experiment kept for reference only.

## Layout

```
src/
  core/scan/       directory scanning and enumeration
  core/rules/      rule engine and risk classification
  core/migrate/    migration: backup, copy, verify, link, crash recovery
  core/rollback/   rollback: preflight, execution, recovery
  core/store/      local store: journal, history, settings, learned rules, logs
  core/ai/         AI analysis interface (off by default)
  platform/        platform adapters: win / mac / posix
data/              IDE catalog and rule data
ui/                static UI prototype
docs/              requirements, decisions, specs, task outputs
tests/             unit tests
tools/             drills and validation scripts
```

## Documentation

1. [Product requirements](docs/功能需求.md)
2. [IDE directory catalog](docs/目录清单.md) — data behind the rule engine
3. [Product spec](docs/specs/产品规格-v1.0.md) — acceptance criteria and safety invariants
4. [Architecture](docs/specs/架构设计-v1.0.md) — modules, interfaces and data flow
5. [UI interaction spec](docs/specs/界面交互说明-v1.0.md) — screens and interaction rules

## License

[PolyForm Noncommercial License 1.0.0](LICENSE). Free for personal, study, research and nonprofit use. **Any commercial use — including internal use by a company — requires a separate commercial license.** Contact 44062290+QuotaFuse-ai@users.noreply.github.com.

This is a source-available license, not an OSI-approved open-source license.

## Safety and privacy

- Migration copies only; the source is never recursively deleted. Deletion targets verified copies and stale links only
- Dangerous directories (IDE installations, user source code) are blocked from migration; yellow-tier directories require explicit confirmation
- AI analysis is off by default. When enabled, directory information is sent to a third-party AI service, and the UI says so before you turn it on
- Temporary scripts in the repo root (`check*.py`, `upload*.py`, `test_ssh.py`, `clean.py`) contain sensitive credentials. They are excluded by `.gitignore` and **must not be committed**

## Contributing

- Open an issue first; report outputs and risks in the issue comment when done
- Keep one logical change per commit and use conventional commit messages
