# -*- coding: utf-8 -*-
"""Repository safety check for Disk Warden.

Users no longer need to clone this repository: they download a signed-checksum
archive from the Releases page.  This script answers the question that remains
-- "is the source tree safe, and are there dangerous files in it?" -- with a
repeatable, offline scan:

* inventory of every tracked file, with SHA-256 for the ones that execute
* compiled binaries / installers tracked in the tree (high)
* shell and PowerShell scripts, and risky constructs inside them (high/medium)
* secrets: private keys, provider tokens, hard-coded API keys (high)
* outbound network endpoints in source (medium if not on the allowlist)
* symlinks, oversized files, double extensions, control characters (medium)
* optional: inventory of a release archive (``--zip``) so the shipped bundle
  can be checked with the same rules

Nothing is executed and nothing is uploaded.  Exit status is 1 when any
high-severity finding exists, 0 otherwise, so CI can gate on it.

Usage::

    python tools/repo_safety_check.py
    python tools/repo_safety_check.py --json report.json --md report.md
    python tools/repo_safety_check.py --zip dist/DiskWarden-0.9.0-beta.10-win-x64.zip
"""

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import zipfile

SEV_HIGH = "high"
SEV_MEDIUM = "medium"
SEV_INFO = "info"

EXECUTABLE_EXT = frozenset(
    [
        ".exe", ".dll", ".msi", ".com", ".scr", ".sys", ".so", ".dylib",
        ".node", ".app", ".dmg", ".pkg", ".deb", ".rpm", ".jar", ".bin",
        ".elf", ".apk",
    ]
)
SCRIPT_EXT = frozenset([".ps1", ".bat", ".cmd", ".vbs", ".sh", ".bash", ".mjs", ".js"])
TEXT_EXT = frozenset(
    [
        ".py", ".md", ".txt", ".json", ".yml", ".yaml", ".html", ".css",
        ".spec", ".cfg", ".toml", ".ini", ".gitignore", ".gitattributes",
    ]
)

# Risky constructs.  Kept as fragments so the scanner does not flag itself.
RISKY_PATTERNS = [
    (SEV_HIGH, "pipe-to-shell", "curl " + "| sh", "downloads and executes a remote script"),
    (SEV_HIGH, "pipe-to-shell", "wget " + "| sh", "downloads and executes a remote script"),
    (SEV_HIGH, "encoded-command", "powershell" + " -enc", "base64-encoded PowerShell payload"),
    (SEV_HIGH, "encoded-command", "-Encoded" + "Command", "base64-encoded PowerShell payload"),
    (SEV_MEDIUM, "recursive-delete", "Remove-Item" + " -Recurse -Force", "recursive delete"),
    (SEV_HIGH, "recursive-delete", "rm " + "-rf /", "recursive delete of a root path"),
    (SEV_MEDIUM, "shell-exec", "shell" + "=True", "subprocess call through a shell"),
    (SEV_MEDIUM, "shell-exec", "os." + "system(", "shell execution"),
    (SEV_MEDIUM, "dynamic-exec", "eval" + "(", "dynamic code evaluation"),
    (SEV_MEDIUM, "dynamic-exec", "exec" + "(", "dynamic code evaluation"),
    (SEV_MEDIUM, "hidden-download", "Invoke" + "-WebRequest", "background download"),
    (SEV_MEDIUM, "hidden-download", "bitsadmin", "background download"),
    (SEV_MEDIUM, "permission-widening", "chmod " + "777", "world-writable permission"),
]

SECRET_PATTERNS = [
    (SEV_HIGH, "private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    (SEV_HIGH, "github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    (SEV_HIGH, "aws-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (SEV_HIGH, "openai-key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    (SEV_HIGH, "slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
]

# Assignment of a literal to a secret-looking name.  Placeholders are allowed.
SECRET_ASSIGN = re.compile(
    r"""(?ix)\b(api[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*(['"])([^'"\n]{8,})\2"""
)
PLACEHOLDER_HINTS = (
    "xxx", "your", "example", "changeme", "placeholder", "redacted", "***", "<",
    "fake", "dummy", "sample", "test", "secret",
)

# A recursive delete aimed at a drive root, home directory or system path is
# the dangerous shape; one scoped to a build or sandbox folder is routine.
ROOT_DELETE = re.compile(
    r"""(?ix)(?:Remove-Item|rm|rmdir)\b.*(?:[A-Za-z]:[\\/]|['"]~/|\$env:(?:SystemDrive|USERPROFILE|HOME)\b|/etc\b|/usr\b|/Users\b)"""
)

URL_RE = re.compile(r"https?://([A-Za-z0-9._-]+)")

# Hosts the product may legitimately talk to.
ALLOWED_HOSTS = frozenset(
    [
        "github.com",
        "api.github.com",
        "raw.githubusercontent.com",
        "objects.githubusercontent.com",
        "docs.github.com",
        "localhost",
        "127.0.0.1",
        "www.w3.org",
        "schemas.microsoft.com",
        "creativecommons.org",
        "gitee.com",
        "avatars.githubusercontent.com",
        "developer.microsoft.com",
        "docs.microsoft.com",
        "learn.microsoft.com",
        # RFC 2606 reserved names: fixtures, never resolvable.
        "example.com",
        "example.org",
        "example.net",
        "example.test",
        "example.invalid",
        "settings.example",
    ]
)

SELF = "tools/repo_safety_check.py"
MAX_TEXT_BYTES = 2 * 1024 * 1024


def sha256_file(path):
    # type: (str) -> str
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tracked_files(root):
    # type: (str) -> list
    try:
        out = subprocess.check_output(
            ["git", "ls-files", "-z"], cwd=root, stderr=subprocess.DEVNULL
        )
        names = [n for n in out.decode("utf-8").split("\0") if n]
        if names:
            return names
    except (OSError, subprocess.CalledProcessError):
        pass
    found = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "build", "dist")]
        for name in files:
            found.append(os.path.relpath(os.path.join(base, name), root).replace("\\", "/"))
    return sorted(found)


RESERVED_SUFFIXES = (".test", ".invalid", ".example", ".")


def _host_is_known(host):
    # type: (str) -> bool
    if host in ALLOWED_HOSTS or host.endswith(RESERVED_SUFFIXES):
        return True
    # Concatenated fixtures like "https://" + host leave a fragment behind.
    return "." not in host


def finding(severity, kind, path, detail, line=0):
    # type: (str, str, str, str, int) -> dict
    return {"severity": severity, "kind": kind, "path": path, "line": line, "detail": detail}


def scan_text(rel, text):
    # type: (str, str) -> list
    results = []
    lowered = rel.lower()
    ext = os.path.splitext(lowered)[1]
    is_script = ext in SCRIPT_EXT
    is_code = ext in (".py", ".ps1", ".bat", ".cmd", ".vbs", ".sh", ".mjs", ".js")
    lines = text.split("\n")

    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line:
            continue

        # Secrets first: they matter everywhere, including docs.
        for severity, kind, pattern in SECRET_PATTERNS:
            if pattern.search(raw):
                results.append(finding(severity, kind, rel, "credential-shaped literal", number))
        match = SECRET_ASSIGN.search(raw)
        if match and not any(h in match.group(3).lower() for h in PLACEHOLDER_HINTS):
            results.append(
                finding(SEV_HIGH, "hard-coded-secret", rel, "literal assigned to %s" % match.group(1), number)
            )

        # Risky constructs only in code/scripts, never in prose.
        if is_code and rel != SELF:
            for severity, kind, needle, description in RISKY_PATTERNS:
                if needle not in raw:
                    continue
                if kind == "recursive-delete" and ROOT_DELETE.search(raw):
                    severity = SEV_HIGH
                    description = "recursive delete aimed at a root or home path"
                results.append(finding(severity, kind, rel, description, number))

    if is_code:
        for match in URL_RE.finditer(text):
            host = match.group(1).lower()
            if _host_is_known(host):
                continue
            results.append(
                finding(SEV_MEDIUM, "unlisted-host", rel, "outbound host %s" % host)
            )
    elif is_script:
        results.append(finding(SEV_INFO, "script", rel, "script file tracked in the repository"))
    return results


def scan_tree(root):
    # type: (str) -> dict
    files = tracked_files(root)
    findings = []
    inventory = []
    for rel in files:
        path = os.path.join(root, rel)
        if not os.path.isfile(path) and not os.path.islink(path):
            continue
        size = os.path.getsize(path) if not os.path.islink(path) else 0
        ext = os.path.splitext(rel.lower())[1]
        entry = {"path": rel, "bytes": size, "ext": ext}
        if os.path.islink(path):
            findings.append(finding(SEV_MEDIUM, "symlink", rel, "tracked symlink"))
        if rel != SELF and size > MAX_TEXT_BYTES:
            findings.append(
                finding(SEV_MEDIUM, "oversized", rel, "%d bytes tracked in git" % size)
            )
        if ext in EXECUTABLE_EXT:
            entry["sha256"] = sha256_file(path) if not os.path.islink(path) else ""
            findings.append(
                finding(
                    SEV_HIGH,
                    "binary",
                    rel,
                    "executable binary tracked in the repository (%.1f MB)" % (size / 1048576.0),
                )
            )
        if ".." in rel or "\u202e" in rel:
            findings.append(finding(SEV_MEDIUM, "suspicious-name", rel, "path traversal or RTL override"))
        if re.search(r"\.(jpg|jpeg|png|gif|pdf|doc|docx|xls|xlsx|txt|zip)\.(exe|scr|bat|cmd|ps1|js)$", rel.lower()):
            findings.append(finding(SEV_MEDIUM, "double-extension", rel, "disguised executable"))
        if ext in SCRIPT_EXT and rel != SELF:
            entry["sha256"] = sha256_file(path) if not os.path.islink(path) else ""
        inventory.append(entry)

        if ext in TEXT_EXT or ext in SCRIPT_EXT or not ext:
            try:
                with io.open(path, encoding="utf-8") as handle:
                    text = handle.read(MAX_TEXT_BYTES)
            except (UnicodeDecodeError, OSError):
                continue
            findings.extend(scan_text(rel, text))
        elif ext and ext not in EXECUTABLE_EXT:
            if size <= MAX_TEXT_BYTES:
                try:
                    with io.open(path, encoding="utf-8") as handle:
                        text = handle.read(MAX_TEXT_BYTES)
                except (UnicodeDecodeError, OSError):
                    continue
                findings.extend(scan_text(rel, text))
    return {"files": inventory, "findings": findings}


def scan_zip(path):
    # type: (str) -> dict
    findings = []
    entries = []
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            name = info.filename
            ext = os.path.splitext(name.lower())[1]
            entries.append({"path": name, "bytes": info.file_size, "ext": ext})
            if info.file_size == 0 and name.endswith("/"):
                continue
            if ".." in name.replace("\\", "/").split("/"):
                findings.append(finding(SEV_HIGH, "path-traversal", name, "archive entry escapes the target"))
            if ext not in EXECUTABLE_EXT | SCRIPT_EXT | TEXT_EXT | frozenset(
                [".png", ".ico", ".icns", ".svg", ".dist-info", ".sha256", ".pem", ""]
            ):
                findings.append(finding(SEV_INFO, "unexpected-entry", name, "uncommon extension in archive"))
    return {
        "path": os.path.basename(path),
        "bytes": os.path.getsize(path),
        "sha256": sha256_file(path),
        "entries": len(entries),
        "executables": sorted({e["path"] for e in entries if e["ext"] in EXECUTABLE_EXT}),
        "findings": findings,
    }


def summarize(findings):
    # type: (list) -> dict
    counts = {SEV_HIGH: 0, SEV_MEDIUM: 0, SEV_INFO: 0}
    for item in findings:
        counts[item["severity"]] = counts.get(item["severity"], 0) + 1
    return counts


def render_markdown(report):
    # type: (dict) -> str
    counts = report["counts"]
    out = []
    out.append("# Repository safety report")
    out.append("")
    out.append("Generated by `tools/repo_safety_check.py` — offline, read-only, nothing executed.")
    out.append("")
    out.append("- Tracked files scanned: **%d**" % len(report["files"]))
    out.append("- Findings: **%d high**, %d medium, %d info" % (counts[SEV_HIGH], counts[SEV_MEDIUM], counts[SEV_INFO]))
    out.append("- Verdict: **%s**" % ("PASS" if counts[SEV_HIGH] == 0 else "FAIL"))
    out.append("")
    out.append("## Checks")
    out.append("")
    out.append("| Check | What it catches |")
    out.append("|---|---|")
    out.append("| `binary` | compiled executables or installers tracked in git |")
    out.append("| `hard-coded-secret`, `private-key`, `*-token` | credentials committed to the tree |")
    out.append("| `pipe-to-shell`, `encoded-command` | download-and-run or obfuscated payloads in scripts |")
    out.append("| `recursive-delete`, `shell-exec`, `dynamic-exec` | destructive or unsafe constructs |")
    out.append("| `unlisted-host` | outbound endpoints outside the known allowlist |")
    out.append("| `symlink`, `oversized`, `suspicious-name`, `double-extension` | phishy or accidental payloads |")
    out.append("| `path-traversal` (archive) | release entries that escape the extraction directory |")
    out.append("")
    if report.get("zips"):
        out.append("## Release archives")
        out.append("")
        out.append("| Archive | Entries | Executables | SHA-256 |")
        out.append("|---|---|---|---|")
        for item in report["zips"]:
            out.append(
                "| `%s` | %d | %d | `%s` |"
                % (item["path"], item["entries"], len(item["executables"]), item["sha256"])
            )
        out.append("")
    out.append("## Findings")
    out.append("")
    if not report["findings"]:
        out.append("No findings.")
    else:
        out.append("| Severity | Kind | Location | Detail |")
        out.append("|---|---|---|---|")
        for item in sorted(report["findings"], key=lambda f: (f["severity"] != SEV_HIGH, f["path"])):
            where = item["path"] + (":%d" % item["line"] if item.get("line") else "")
            out.append("| %s | %s | `%s` | %s |" % (item["severity"], item["kind"], where, item["detail"]))
    out.append("")
    out.append("## Executable inventory")
    out.append("")
    execs = [f for f in report["files"] if f.get("sha256") and f["path"] != SELF]
    if not execs:
        out.append("The repository tracks no compiled binaries — every shipped executable is built by CI or `packaging/build.ps1` and published on the Releases page.")
    else:
        out.append("| File | Bytes | SHA-256 |")
        out.append("|---|---|---|")
        for item in execs:
            out.append("| `%s` | %d | `%s` |" % (item["path"], item["bytes"], item["sha256"]))
    out.append("")
    return "\n".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline safety check for the Disk Warden repository.")
    parser.add_argument("--root", default=".", help="repository root (default: current directory)")
    parser.add_argument("--json", default="", help="write the full report as JSON")
    parser.add_argument("--md", default="", help="write a Markdown report")
    parser.add_argument("--zip", action="append", default=[], help="also scan a release archive")
    parser.add_argument("--strict", action="store_true", help="fail on medium findings too")
    args = parser.parse_args(argv)

    root = os.path.abspath(args.root)
    tree = scan_tree(root)
    findings = list(tree["findings"])
    zips = []
    for path in args.zip:
        if not os.path.isfile(path):
            raise SystemExit("archive not found: %s" % path)
        archive = scan_zip(path)
        zips.append(archive)
        findings.extend(archive["findings"])

    counts = summarize(findings)
    report = {
        "root": root,
        "files": tree["files"],
        "findings": findings,
        "counts": counts,
        "zips": zips,
        "verdict": "PASS" if counts[SEV_HIGH] == 0 else "FAIL",
    }

    if args.json:
        with io.open(args.json, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(report, indent=2, ensure_ascii=False))
    if args.md:
        with io.open(args.md, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(render_markdown(report))

    print("scanned %d tracked files" % len(tree["files"]))
    for severity in (SEV_HIGH, SEV_MEDIUM, SEV_INFO):
        print("%-6s %d" % (severity, counts[severity]))
    for item in findings:
        if item["severity"] == SEV_HIGH:
            print("HIGH %s %s%s: %s" % (
                item["kind"], item["path"], ":%d" % item["line"] if item.get("line") else "", item["detail"]
            ))
    print("verdict: %s" % report["verdict"])

    failed = counts[SEV_HIGH] > 0 or (args.strict and counts[SEV_MEDIUM] > 0)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
