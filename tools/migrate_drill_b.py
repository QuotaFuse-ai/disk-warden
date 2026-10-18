#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sandbox drills for migration engine B (T10).

Every drill builds its own tree under a temporary directory: the real
``%APPDATA%``/``%LOCALAPPDATA%`` trees are never read or written
(architecture 6.9).  The copy half and the link half run against the real
robocopy and ``mklink /J`` adapters, so these drills exercise the actual OS
behaviour instead of an in-memory simulation:

  success           prepare_copy -> complete_migration; the source becomes a
                    junction whose read-back target resolves to the final
                    path, a probe file reads through the link, the tombstone
                    and the staging directory are gone
  link_failure      junction creation is made to fail; the engine must
                    restore the source and remove the promoted target
  crash_before_link prepare_copy + S5 + S6, then recover_job; the source must
                    be restored, the target discarded, and a rerun is a no-op
  crash_after_link  prepare_copy + S5 + S6 + S7, then recover_job; the link
                    is verified, the tombstone removed, and a rerun is a no-op

Exit code is 0 when all drills pass, 1 otherwise.  JSON is printed to stdout.

Usage:
    python tools/migrate_drill_b.py
    python tools/migrate_drill_b.py --keep
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
    ERROR_LINK,
    STATE_DONE,
    STATE_LINKED,
    STATE_ROLLED_BACK,
    STATE_SOURCE_RENAMED,
    MigrationOps,
    MigrationRequest,
    complete_migration,
    discard_staging,
    ensure_link,
    ensure_source_renamed,
    ensure_target_ready,
    prepare_copy,
    recover_job,
    write_owner_marker,
)
from src.platform import (  # noqa: E402
    is_junction,
    link_target_matches,
    read_junction_target,
)

JOB_ID = "0123456789abcdef0123456789abcdef"
JOB8 = "01234567"
CONFIG_NAMES = ("User/settings.json", "User/keybindings.json")
LINK_STAGES = ("swap", "link", "link_verify", "cleanup")


def sha256(path):
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def tree_state(root, skip_owner_markers=False):
    """Return ``{relative posix path: [size, sha256]}`` for regular files."""
    root = Path(root)
    state = {}
    for path in sorted(root.rglob("*")):
        if skip_owner_markers and path.name.startswith(".dw-owner-"):
            continue
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


def tombstone_for(sandbox):
    return sandbox["tool_root"] / (".dwtombstone-%s" % JOB8)


def failure(message):
    raise AssertionError(message)


def done_stages(events):
    return [
        event["stage"]
        for event in events
        if event.get("status") == "done" and event.get("stage") in LINK_STAGES
    ]


def check_source_tree(sandbox, before):
    after = tree_state(sandbox["source"], skip_owner_markers=True)
    if before != after:
        failure("source data does not match the pre-migration snapshot")


class FailingLinkOps(MigrationOps):
    """Real ops with only junction creation broken (fault injection)."""

    def create_link(self, link_path, target_path):
        raise OSError("injected junction failure")


def stage_to_crash(sandbox, stop_after):
    """Mimic complete_migration up to ``rename`` or ``link`` and stop there."""
    prepared = prepare_copy(request_for(sandbox))
    if not prepared.ok:
        failure("prepare_copy failed: %s" % prepared.error_message)

    source = Path(prepared.source_path)
    final = Path(prepared.final_path)
    staging = Path(prepared.staging_path)
    ops = MigrationOps()

    write_owner_marker(
        str(staging),
        JOB_ID,
        "demo",
        str(source),
        str(final),
        file_count=prepared.verify.source_file_count,
        total_bytes=prepared.verify.source_bytes,
    )
    ensure_target_ready(ops, str(staging), str(final), JOB_ID)
    discard_staging(str(staging), JOB_ID)
    ensure_source_renamed(ops, str(source), str(tombstone_for(sandbox)), JOB_ID)
    if stop_after == "link":
        ensure_link(ops, str(source), str(final))
    return prepared


def drill_success(base):
    sandbox = build_sandbox(base)
    source = sandbox["source"]
    before = tree_state(source, skip_owner_markers=True)
    events = []

    prepared = prepare_copy(request_for(sandbox), progress_cb=events.append)
    if not prepared.ok:
        failure("success drill copy failed: %s" % prepared.error_message)
    completion = complete_migration(
        prepared, process_names=(), progress_cb=events.append
    )

    if completion.state != STATE_DONE:
        failure(
            "completion state is %s (%s)"
            % (completion.state, completion.error_message)
        )
    if not is_junction(str(source)):
        failure("source is not a junction after completion")
    target = read_junction_target(str(source))
    final = Path(prepared.final_path)
    if not link_target_matches(target, str(final)):
        failure("junction read-back %r does not resolve to %r" % (target, str(final)))
    if not completion.link_verified:
        failure("engine did not verify the junction")
    if not (final / "level1" / "data.bin").is_file():
        failure("promoted target is missing data")

    probe = source / "level1" / "data.bin"
    expected_probe = bytes(range(256)) * 4096
    if probe.read_bytes() != expected_probe:
        failure("probe read through the junction differs from the source data")
    check_source_tree(sandbox, before)

    tombstone = tombstone_for(sandbox)
    if tombstone.exists():
        failure("tombstone survived a successful migration")
    staging_parent = Path(prepared.staging_path).parent
    if staging_parent.exists():
        failure("staging directory survived a successful migration")

    stages = done_stages(events)
    if stages != list(LINK_STAGES):
        failure("stage sequence is %r, expected %r" % (stages, LINK_STAGES))

    return {
        "ok": True,
        "state": completion.state,
        "link_target": target,
        "link_matches_final": True,
        "link_verified": completion.link_verified,
        "probe_read": "level1/data.bin",
        "source_removed": completion.source_removed,
        "tombstone_removed": True,
        "staging_removed": True,
        "event_stages": stages,
    }


def drill_link_failure(base):
    sandbox = build_sandbox(base)
    source = sandbox["source"]
    before = tree_state(source, skip_owner_markers=True)
    events = []

    prepared = prepare_copy(request_for(sandbox))
    if not prepared.ok:
        failure("link-failure drill copy failed: %s" % prepared.error_message)
    completion = complete_migration(
        prepared,
        process_names=(),
        ops=FailingLinkOps(),
        progress_cb=events.append,
    )

    if completion.state != STATE_ROLLED_BACK:
        failure("state is %s, expected %s" % (completion.state, STATE_ROLLED_BACK))
    if completion.error_code != ERROR_LINK:
        failure("error code is %r, expected %r" % (completion.error_code, ERROR_LINK))
    if not completion.rolled_back:
        failure("completion did not report a rollback")
    if is_junction(str(source)):
        failure("a junction was left behind after a link failure")
    if not source.is_dir():
        failure("source directory was not restored")
    check_source_tree(sandbox, before)

    final = Path(prepared.final_path)
    if final.exists():
        failure("promoted target was not removed after the rollback")
    if tombstone_for(sandbox).exists():
        failure("tombstone was left behind after the rollback")
    if Path(prepared.staging_path).parent.exists():
        failure("staging directory survived the rollback")

    failures = [
        event
        for event in events
        if event.get("stage") == "link" and event.get("status") == "failed"
    ]
    if not failures:
        failure("no failed link event was emitted")
    if failures[0].get("error_code") != ERROR_LINK:
        failure("link failure event carries %r" % failures[0].get("error_code"))

    return {
        "ok": False,
        "state": completion.state,
        "error_code": completion.error_code,
        "rolled_back": True,
        "source_restored": True,
        "target_removed": True,
        "tombstone_removed": True,
        "staging_removed": True,
        "link_event": failures[0],
    }


def drill_crash_before_link(base):
    sandbox = build_sandbox(base)
    source = sandbox["source"]
    before = tree_state(source, skip_owner_markers=True)

    prepared = stage_to_crash(sandbox, "rename")
    if source.exists():
        failure("crash setup did not rename the source")
    if not tombstone_for(sandbox).is_dir():
        failure("crash setup did not leave a tombstone")
    if not (Path(prepared.final_path) / "level1" / "data.bin").is_file():
        failure("crash setup did not promote the target")

    events = []
    recovered = recover_job(
        JOB_ID,
        "demo",
        str(source),
        prepared.final_path,
        staging_path=prepared.staging_path,
        recorded_state=STATE_SOURCE_RENAMED,
        progress_cb=events.append,
    )
    if recovered.state != STATE_ROLLED_BACK:
        failure(
            "recovery state is %s (%s)"
            % (recovered.state, recovered.error_message)
        )
    if is_junction(str(source)) or not source.is_dir():
        failure("recovery did not restore a real source directory")
    check_source_tree(sandbox, before)
    if Path(prepared.final_path).exists():
        failure("recovery did not remove the promoted target")
    if tombstone_for(sandbox).exists():
        failure("recovery left the tombstone behind")
    if Path(prepared.staging_path).parent.exists():
        failure("recovery left the staging directory behind")

    again = recover_job(
        JOB_ID,
        "demo",
        str(source),
        prepared.final_path,
        staging_path=prepared.staging_path,
        recorded_state=STATE_ROLLED_BACK,
    )
    if again.state != STATE_ROLLED_BACK:
        failure("second recovery changed the state to %s" % again.state)
    check_source_tree(sandbox, before)

    return {
        "ok": True,
        "first_state": recovered.state,
        "second_state": again.state,
        "idempotent": True,
        "source_restored": True,
        "target_removed": True,
        "recover_events": len(events),
    }


def drill_crash_after_link(base):
    sandbox = build_sandbox(base)
    source = sandbox["source"]
    before = tree_state(source, skip_owner_markers=True)

    prepared = stage_to_crash(sandbox, "link")
    if not is_junction(str(source)):
        failure("crash setup did not create a junction")
    if not tombstone_for(sandbox).is_dir():
        failure("crash setup did not leave a tombstone")

    recovered = recover_job(
        JOB_ID,
        "demo",
        str(source),
        prepared.final_path,
        staging_path=prepared.staging_path,
        recorded_state=STATE_LINKED,
    )
    if recovered.state != STATE_DONE:
        failure(
            "recovery state is %s (%s)"
            % (recovered.state, recovered.error_message)
        )
    if not recovered.link_verified:
        failure("recovery did not verify the junction")
    target = read_junction_target(str(source))
    if not link_target_matches(target, prepared.final_path):
        failure("recovered junction points to %r" % target)
    if tombstone_for(sandbox).exists():
        failure("recovery did not remove the tombstone")
    if Path(prepared.staging_path).parent.exists():
        failure("recovery left the staging directory behind")
    check_source_tree(sandbox, before)

    again = recover_job(
        JOB_ID,
        "demo",
        str(source),
        prepared.final_path,
        staging_path=prepared.staging_path,
        recorded_state=STATE_DONE,
    )
    if again.state != STATE_DONE:
        failure("second recovery changed the state to %s" % again.state)
    if not is_junction(str(source)):
        failure("second recovery broke the junction")
    check_source_tree(sandbox, before)

    return {
        "ok": True,
        "first_state": recovered.state,
        "second_state": again.state,
        "idempotent": True,
        "link_verified": True,
        "tombstone_removed": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep", action="store_true", help="keep the sandbox for inspection"
    )
    arguments = parser.parse_args()

    root = Path(tempfile.mkdtemp(prefix="dw-t10-drill-"))
    report = {"sandbox": str(root), "drills": {}}
    failed = False
    try:
        for name, drill in (
            ("success", drill_success),
            ("link_failure", drill_link_failure),
            ("crash_before_link", drill_crash_before_link),
            ("crash_after_link", drill_crash_after_link),
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
