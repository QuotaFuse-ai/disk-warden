#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sandbox drills for migration engine A (T09).

Every drill builds its own tree under a temporary directory: the real
``%APPDATA%``/``%LOCALAPPDATA%`` trees are never read or written
(architecture 6.9).  Three scenarios run with the real robocopy adapter:

  success   backup -> robocopy -> verify, source untouched
  mismatch  a staging file is deliberately corrupted after robocopy;
            verification must fail, staging must be removed, source kept
  bad_exit  robocopy is pointed at an invalid destination and reports a real
            exit code >= 8; the engine must abort and remove staging

Exit code is 0 when all drills pass, 1 otherwise.  JSON is printed to stdout.

Usage:
    python tools/migrate_drill.py
    python tools/migrate_drill.py --keep
"""

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.migrate import (  # noqa: E402
    ERROR_COPY,
    ERROR_VERIFY,
    MigrationRequest,
    prepare_copy,
)
from src.platform import run_robocopy  # noqa: E402

JOB_ID = "0123456789abcdef0123456789abcdef"
CONFIG_NAMES = ("User/settings.json", "User/keybindings.json")
CORRUPTION = b"drill-corruption"


def sha256(path):
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def tree_state(root):
    """Return ``{relative posix path: [size, sha256]}`` for regular files."""
    root = Path(root)
    state = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            state[path.relative_to(root).as_posix()] = [
                path.stat().st_size,
                sha256(path),
            ]
    return state


def build_sandbox(base):
    base = Path(base)
    tool_root = base / "Cursor"
    cache = tool_root / "Cache"
    (cache / "level1" / "sub").mkdir(parents=True)
    (cache / "empty").mkdir()
    (cache / "level1" / "data.bin").write_bytes(bytes(range(256)) * 4096)
    (cache / "level1" / "sub" / "more.bin").write_bytes(b"m" * (512 * 1024))
    user = tool_root / "User"
    user.mkdir()
    (user / "settings.json").write_text('{"theme": "dark"}', encoding="utf-8")
    (user / "keybindings.json").write_text("[]", encoding="utf-8")
    target_root = base / "target" / "data"
    target_root.mkdir(parents=True)
    backup_root = base / "target" / "backup"
    return {
        "base": base,
        "tool_root": tool_root,
        "source": cache,
        "target_root": target_root,
        "backup_root": backup_root,
    }


def request_for(sandbox):
    return MigrationRequest(
        tool_id="demo",
        source_path=str(sandbox["source"]),
        target_root=str(sandbox["target_root"]),
        backup_root=str(sandbox["backup_root"]),
        config_root=str(sandbox["tool_root"]),
        job_id=JOB_ID,
    )


def staging_parent(sandbox):
    return sandbox["target_root"] / "demo" / ".dw-partial-01234567"


def failure(message):
    raise AssertionError(message)


def check_source_untouched(before, sandbox):
    after = tree_state(sandbox["source"])
    if before != after:
        failure("source tree changed during the drill")
    if not sandbox["source"].is_dir():
        failure("source directory disappeared")


def drill_success(base):
    sandbox = build_sandbox(base)
    before = tree_state(sandbox["source"])
    events = []

    result = prepare_copy(request_for(sandbox), progress_cb=events.append)

    if not result.ok:
        failure("success drill failed: %s" % result.error_message)
    check_source_untouched(before, sandbox)

    staging = Path(result.staging_path)
    if not (staging / "level1" / "data.bin").is_file():
        failure("staged file is missing")
    if not (staging / "level1" / "sub" / "more.bin").is_file():
        failure("staged nested file is missing")
    if not (staging / "empty").is_dir():
        failure("staged empty directory is missing")

    manifest_path = Path(result.backup_dir) / "manifest.json"
    if not manifest_path.is_file():
        failure("backup manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = sorted(entry["rel_path"] for entry in manifest["entries"])
    if entries != sorted(CONFIG_NAMES):
        failure("backup entries are %r, expected %r" % (entries, CONFIG_NAMES))
    if int(manifest["file_count"]) != len(CONFIG_NAMES):
        failure("backup file_count does not match the manifest")
    for entry in manifest["entries"]:
        backed_up = Path(result.backup_dir) / "files" / entry["rel_path"]
        if not backed_up.is_file():
            failure("backup copy is missing: %s" % entry["rel_path"])
        if sha256(backed_up) != entry["sha256"]:
            failure("backup hash mismatch: %s" % entry["rel_path"])

    log_path = staging.parent / "robocopy.log"
    if not log_path.is_file():
        failure("robocopy log is missing")

    stages = [event["stage"] for event in events if event["status"] == "done"]
    expected_stages = ["prepare", "backup", "copy", "verify"]
    if stages != expected_stages:
        failure("stage sequence is %r, expected %r" % (stages, expected_stages))

    return {
        "ok": True,
        "error_code": result.error_code,
        "copy_exit_code": result.copy.exit_code,
        "verify": {
            "files": [result.verify.source_file_count, result.verify.target_file_count],
            "bytes": [result.verify.source_bytes, result.verify.target_bytes],
        },
        "backup_entries": entries,
        "backup_dir": result.backup_dir,
        "final_path": result.final_path,
        "staging_kept": staging.is_dir(),
        "source_untouched": True,
        "event_stages": stages,
    }


def drill_verify_mismatch(base):
    sandbox = build_sandbox(base)
    before = tree_state(sandbox["source"])

    def corrupting_transfer(src, dst, log_path, cancel_token=None):
        exit_code = run_robocopy(src, dst, log_path, cancel_token)
        with open(str(Path(dst) / "level1" / "data.bin"), "ab") as handle:
            handle.write(CORRUPTION)
        return exit_code

    result = prepare_copy(
        request_for(sandbox), transfer=corrupting_transfer
    )

    if result.ok:
        failure("corrupted copy was reported as successful")
    if result.error_code != ERROR_VERIFY:
        failure("error code is %r, expected %r" % (result.error_code, ERROR_VERIFY))
    if result.verify is None or result.verify.size_mismatch_count != 1:
        failure("verification did not report exactly one size mismatch")
    if result.verify.target_bytes != result.verify.source_bytes + len(CORRUPTION):
        failure("unexpected byte totals after corruption")
    if not result.temp_removed:
        failure("staging was not removed after a verification failure")
    if staging_parent(sandbox).exists():
        failure("staging directory survived a verification failure")
    check_source_untouched(before, sandbox)
    if not any(sandbox["backup_root"].iterdir()):
        failure("backup was removed even though the copy failed")

    return {
        "ok": False,
        "error_code": result.error_code,
        "error_message": result.error_message,
        "verify": {
            "files": [result.verify.source_file_count, result.verify.target_file_count],
            "bytes": [result.verify.source_bytes, result.verify.target_bytes],
            "size_mismatches": result.verify.size_mismatch_count,
        },
        "staging_removed": result.temp_removed,
        "source_untouched": True,
        "backup_kept": True,
    }


def drill_robocopy_bad_exit(base):
    sandbox = build_sandbox(base)
    before = tree_state(sandbox["source"])
    blocker = sandbox["base"] / "not-a-directory"
    blocker.write_text("a file, not a directory", encoding="utf-8")

    def bad_destination_transfer(src, dst, log_path, cancel_token=None):
        return run_robocopy(src, str(blocker), log_path, cancel_token)

    result = prepare_copy(
        request_for(sandbox), transfer=bad_destination_transfer
    )

    if result.ok:
        failure("robocopy failure was reported as successful")
    if result.error_code != ERROR_COPY:
        failure("error code is %r, expected %r" % (result.error_code, ERROR_COPY))
    if result.copy is None or result.copy.exit_code < 8:
        failure("drill did not produce a real robocopy exit code >= 8")
    if not result.temp_removed:
        failure("staging was not removed after a robocopy failure")
    if staging_parent(sandbox).exists():
        failure("staging directory survived a robocopy failure")
    check_source_untouched(before, sandbox)

    return {
        "ok": False,
        "error_code": result.error_code,
        "robocopy_exit_code": result.copy.exit_code,
        "staging_removed": result.temp_removed,
        "source_untouched": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep", action="store_true", help="keep the sandbox for inspection"
    )
    arguments = parser.parse_args()

    root = Path(tempfile.mkdtemp(prefix="dw-t09-drill-"))
    report = {"sandbox": str(root), "drills": {}}
    failed = False
    try:
        for name, drill in (
            ("success", drill_success),
            ("verify_mismatch", drill_verify_mismatch),
            ("robocopy_bad_exit", drill_robocopy_bad_exit),
        ):
            base = root / name
            base.mkdir()
            try:
                report["drills"][name] = drill(base)
            except AssertionError as exc:
                failed = True
                report["drills"][name] = {
                    "ok": False,
                    "drill_failed": str(exc),
                }
    finally:
        if arguments.keep:
            print("sandbox kept at %s" % root, file=sys.stderr)
        else:
            shutil.rmtree(str(root), ignore_errors=True)

    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
