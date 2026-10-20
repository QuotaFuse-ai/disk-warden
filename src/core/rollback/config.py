# -*- coding: utf-8 -*-
"""Restore configuration files from a migration backup (AC-ROL-06).

The manifest written by ``migrate.backup`` carries one entry per file with its
size and SHA-256.  A restore never trusts the manifest blindly: the backup
file is hashed again, the current file is compared by content and the chosen
policy is applied:

* A current == backup  -> keep (nothing to do, no rewrite);
* B current missing    -> restore from the backup;
* C user modified      -> keep by default; restore only when the caller
  explicitly asks for ``CONFIG_RESTORE``;
* D backup missing or corrupt -> report ``error`` and name the file; the data
  rollback still completes (config status is reported separately).

The manifest is located by the migration ``job_id``: the caller may hand over
the per-job backup directory or the backup root (the migration journal used to
record the root).  The timestamp in a backup directory name is the migration
time and is never used to build a path.

A config failure never deletes an existing file and never changes the data
outcome.  All writes go through ``atomic_write_bytes``.

Python 3.8 compatible (decision 0005).
"""

import hashlib
import os
from typing import Dict, List, Optional, Tuple

from ..migrate import paths as migrate_paths
from ..store.atomic import StoreError, atomic_write_bytes, read_json_file
from .models import (
    CONFIG_ACTION_BACKUP_CORRUPT,
    CONFIG_ACTION_BACKUP_MISSING,
    CONFIG_ACTION_IDENTICAL,
    CONFIG_ACTION_KEPT_USER,
    CONFIG_ACTION_RESTORED_MISSING,
    CONFIG_ACTION_RESTORED_USER_CHOICE,
    CONFIG_ACTION_RESTORE_FAILED,
    CONFIG_CHOICES,
    CONFIG_KEEP,
    CONFIG_RESTORE,
    CONFIG_STATUS_ERROR,
    CONFIG_STATUS_RESTORED,
    CONFIG_STATUS_SKIPPED,
    ConfigRestoreOutcome,
)

MANIFEST_NAME = "manifest.json"
FILES_DIR_NAME = "files"
CHUNK_SIZE = 64 * 1024


def _sha256_file(path):
    # type: (str) -> Tuple[str, int]
    hasher = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)
            size += len(chunk)
    return hasher.hexdigest(), size


def _safe_rel_path(rel_path):
    # type: (object) -> Optional[str]
    """Reject absolute paths, drive letters and any ``..`` component."""
    if not isinstance(rel_path, str) or not rel_path.strip():
        return None
    normalised = rel_path.replace("\\", "/")
    if normalised.startswith("/") or os.path.isabs(normalised):
        return None
    parts = normalised.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            return None
        if os.path.splitdrive(part)[1] != part:
            return None
    return "/".join(parts)


def _target_path(source_root, rel_path):
    # type: (str, str) -> Optional[str]
    candidate = os.path.join(source_root, *rel_path.split("/"))
    candidate = os.path.abspath(os.path.normpath(candidate))
    if not migrate_paths.is_inside(candidate, source_root):
        return None
    if migrate_paths.same_path(candidate, source_root):
        return None
    return candidate


def _load_manifest(directory):
    # type: (str) -> Tuple[Optional[Dict[str, object]], str]
    manifest_path = os.path.join(directory, MANIFEST_NAME)
    try:
        payload = read_json_file(manifest_path)
    except FileNotFoundError:
        return None, "manifest is missing"
    except (StoreError, OSError) as exc:
        return None, "manifest is unreadable: %s" % exc
    if not isinstance(payload, dict):
        return None, "manifest has an unexpected shape"
    return payload, ""


def _manifest_dir(path):
    # type: (str) -> str
    """Accept either a backup directory or a path to its manifest file."""
    if os.path.isfile(path) and os.path.basename(path).lower() == MANIFEST_NAME:
        return os.path.dirname(path)
    return path


def _has_manifest(directory):
    # type: (str) -> bool
    return os.path.isfile(os.path.join(directory, MANIFEST_NAME))


def _name_matches_job(name, job_id):
    # type: (str, str) -> bool
    try:
        return migrate_paths.is_backup_dir_name(name, job_id)
    except (ValueError, OSError):
        return False


def _candidate_dirs(backup_dir, expected_job_id):
    # type: (str, Optional[str]) -> List[str]
    candidates = []  # type: List[str]
    if _has_manifest(backup_dir):
        candidates.append(backup_dir)
    try:
        with os.scandir(backup_dir) as entries:
            children = [entry.name for entry in entries if entry.is_dir()]
    except OSError:
        children = []
    if expected_job_id:
        children.sort(
            key=lambda name: (
                0 if _name_matches_job(name, expected_job_id) else 1,
                name.lower(),
            )
        )
    else:
        children.sort(key=lambda name: name.lower())
    for name in children:
        directory = os.path.join(backup_dir, name)
        if _has_manifest(directory):
            candidates.append(directory)
    return candidates


def _manifest_job_matches(directory, backup_dir, manifest, expected_job_id):
    # type: (str, str, Dict[str, object], Optional[str]) -> bool
    if not expected_job_id:
        return True
    recorded = manifest.get("job_id")
    if isinstance(recorded, str) and recorded.strip():
        return recorded.strip().lower() == expected_job_id.lower()
    # Older manifests may omit the job id: trust the direct directory or a
    # directory whose name ends with the migration job prefix.
    return directory == backup_dir or _name_matches_job(
        os.path.basename(directory), expected_job_id
    )


def locate_manifest(backup_dir, expected_job_id=None, expected_tool_id=None):
    # type: (str, Optional[str], Optional[str]) -> Tuple[str, Optional[Dict[str, object]], str]
    """Find the manifest for ``expected_job_id``.

    Returns ``(directory, manifest, error)``.  The supplied ``backup_dir`` may
    be the per-job directory or the backup root; in the latter case children
    are scanned and matched by the migration job id (and tool id when given),
    never by a timestamped name.
    """
    backup_dir = _manifest_dir(os.path.abspath(os.path.normpath(backup_dir)))
    candidates = _candidate_dirs(backup_dir, expected_job_id)
    problems = []  # type: List[str]
    for directory in candidates:
        manifest, error = _load_manifest(directory)
        manifest_path = os.path.join(directory, MANIFEST_NAME)
        if manifest is None:
            problems.append("%s: %s" % (manifest_path, error))
            continue
        if not _manifest_job_matches(
            directory, backup_dir, manifest, expected_job_id
        ):
            problems.append(
                "%s: manifest belongs to job %r"
                % (manifest_path, manifest.get("job_id"))
            )
            continue
        tool_id = manifest.get("tool_id")
        if (
            expected_tool_id
            and tool_id not in (None, "", expected_tool_id)
        ):
            problems.append(
                "%s: manifest belongs to tool %r"
                % (manifest_path, tool_id)
            )
            continue
        return directory, manifest, ""

    if problems:
        error = problems[0]
        if len(problems) > 1:
            error += " (and %d more)" % (len(problems) - 1)
        return "", None, error
    searched = (
        "the job's backup directory or the backup root"
        if expected_job_id
        else "the backup directory"
    )
    return (
        "",
        None,
        "no configuration backup manifest found in %s (searched %d "
        "directories); expected %s" % (backup_dir, len(candidates), searched),
    )


def _restore_file(target, payload):
    # type: (str, bytes) -> None
    atomic_write_bytes(target, payload)


def _entry_detail(path, action, **extra):
    # type: (str, str, object) -> Dict[str, object]
    detail = {"path": path, "action": action}
    detail.update(extra)
    return detail


def _error_reason(outcome, details):
    # type: (ConfigRestoreOutcome, List[Dict[str, object]]) -> str
    bad = outcome.failed or outcome.unavailable
    path = bad[0] if bad else ""
    detail = {}
    for item in details:
        if item.get("path") == path and item.get("action") != CONFIG_ACTION_IDENTICAL:
            if item.get("action") in (
                CONFIG_ACTION_BACKUP_CORRUPT,
                CONFIG_ACTION_BACKUP_MISSING,
                CONFIG_ACTION_RESTORE_FAILED,
            ):
                detail = item
                break
    label = path or "manifest entry"
    message = detail.get("reason")
    if message:
        return "cannot restore %s: %s" % (label, message)
    return "cannot restore %s" % label


def restore_configs(
    backup_dir, choice=CONFIG_KEEP, expected_tool_id=None, expected_job_id=None
):
    # type: (Optional[str], str, Optional[str], Optional[str]) -> ConfigRestoreOutcome
    """Apply AC-ROL-06 A/B/C/D to every file listed in the backup manifest."""
    outcome = ConfigRestoreOutcome()
    if choice not in CONFIG_CHOICES:
        choice = CONFIG_KEEP

    if not backup_dir:
        outcome.status = CONFIG_STATUS_SKIPPED
        outcome.reason = (
            "no configuration backup was recorded for this job; "
            "nothing to restore"
        )
        return outcome

    backup_dir, manifest, error = locate_manifest(
        backup_dir, expected_job_id, expected_tool_id
    )
    if manifest is None:
        outcome.status = CONFIG_STATUS_ERROR
        outcome.complete = False
        outcome.reason = error
        return outcome
    outcome.backup_dir = backup_dir
    outcome.manifest_path = os.path.join(backup_dir, MANIFEST_NAME)

    source_root = manifest.get("source_root")
    if not isinstance(source_root, str) or not source_root.strip():
        outcome.status = CONFIG_STATUS_ERROR
        outcome.complete = False
        outcome.reason = "backup manifest has no source root: %s" % (
            outcome.manifest_path,
        )
        return outcome
    source_root = os.path.abspath(os.path.normpath(source_root))

    entries = manifest.get("entries")
    if not isinstance(entries, list):
        outcome.status = CONFIG_STATUS_ERROR
        outcome.complete = False
        outcome.reason = "backup manifest has no entry list: %s" % (
            outcome.manifest_path,
        )
        return outcome

    if not entries:
        outcome.status = CONFIG_STATUS_SKIPPED
        outcome.reason = (
            "the migration did not change any configuration file; "
            "nothing to restore"
        )
        return outcome

    for raw in entries:
        if not isinstance(raw, dict):
            outcome.complete = False
            outcome.unavailable.append("")
            outcome.details.append(
                _entry_detail(
                    "", CONFIG_ACTION_BACKUP_CORRUPT, reason="entry is not an object"
                )
            )
            continue
        rel = _safe_rel_path(raw.get("rel_path"))
        if rel is None:
            outcome.complete = False
            outcome.unavailable.append(str(raw.get("rel_path") or ""))
            outcome.details.append(
                _entry_detail(
                    str(raw.get("rel_path") or ""),
                    CONFIG_ACTION_BACKUP_CORRUPT,
                    reason="unsafe relative path",
                )
            )
            continue
        target = _target_path(source_root, rel)
        if target is None:
            outcome.complete = False
            outcome.unavailable.append(rel)
            outcome.details.append(
                _entry_detail(
                    rel,
                    CONFIG_ACTION_BACKUP_CORRUPT,
                    reason="path escapes the recorded source root",
                )
            )
            continue

        backup_file = os.path.join(backup_dir, FILES_DIR_NAME, *rel.split("/"))
        expected_size = raw.get("size")
        expected_digest = raw.get("sha256")
        try:
            actual_digest, actual_size = _sha256_file(backup_file)
        except FileNotFoundError:
            outcome.complete = False
            outcome.unavailable.append(rel)
            outcome.details.append(
                _entry_detail(
                    rel, CONFIG_ACTION_BACKUP_MISSING, reason="backup file is missing"
                )
            )
            continue
        except OSError as exc:
            outcome.complete = False
            outcome.unavailable.append(rel)
            outcome.details.append(
                _entry_detail(
                    rel, CONFIG_ACTION_BACKUP_CORRUPT, reason=str(exc)
                )
            )
            continue

        if (
            not isinstance(expected_digest, str)
            or not expected_digest
            or expected_digest.lower() != actual_digest
            or not isinstance(expected_size, int)
            or expected_size != actual_size
        ):
            outcome.complete = False
            outcome.unavailable.append(rel)
            outcome.details.append(
                _entry_detail(
                    rel,
                    CONFIG_ACTION_BACKUP_CORRUPT,
                    reason="checksum or size mismatch",
                    expected_sha256=expected_digest,
                    sha256=actual_digest,
                )
            )
            continue

        try:
            with open(backup_file, "rb") as handle:
                payload = handle.read()
        except OSError as exc:
            outcome.complete = False
            outcome.unavailable.append(rel)
            outcome.details.append(
                _entry_detail(
                    rel, CONFIG_ACTION_BACKUP_CORRUPT, reason=str(exc)
                )
            )
            continue

        if os.path.isfile(target):
            try:
                current_digest, _current_size = _sha256_file(target)
            except OSError as exc:
                outcome.complete = False
                outcome.unavailable.append(rel)
                outcome.details.append(
                    _entry_detail(
                        rel,
                        CONFIG_ACTION_BACKUP_CORRUPT,
                        reason="cannot read the current file: %s" % exc,
                    )
                )
                continue
            if current_digest == actual_digest:
                outcome.kept.append(rel)
                outcome.checksums[rel] = actual_digest
                outcome.details.append(
                    _entry_detail(
                        rel,
                        CONFIG_ACTION_IDENTICAL,
                        sha256=actual_digest,
                        verified=True,
                    )
                )
                continue
            if choice != CONFIG_RESTORE:
                outcome.kept.append(rel)
                outcome.checksums[rel] = actual_digest
                outcome.details.append(
                    _entry_detail(
                        rel,
                        CONFIG_ACTION_KEPT_USER,
                        sha256=actual_digest,
                        current_sha256=current_digest,
                        verified=True,
                    )
                )
                continue
            action = CONFIG_ACTION_RESTORED_USER_CHOICE
        else:
            action = CONFIG_ACTION_RESTORED_MISSING

        try:
            _restore_file(target, payload)
            written_digest, written_size = _sha256_file(target)
        except (StoreError, OSError) as exc:
            outcome.failed.append(rel)
            outcome.details.append(
                _entry_detail(
                    rel, CONFIG_ACTION_RESTORE_FAILED, reason=str(exc)
                )
            )
            continue
        if written_digest != actual_digest or written_size != actual_size:
            outcome.failed.append(rel)
            outcome.details.append(
                _entry_detail(
                    rel,
                    CONFIG_ACTION_RESTORE_FAILED,
                    reason="the written file does not match the backup",
                    expected_sha256=actual_digest,
                    sha256=written_digest,
                )
            )
            continue
        outcome.restored.append(rel)
        outcome.checksums[rel] = written_digest
        outcome.details.append(
            _entry_detail(
                rel, action, sha256=written_digest, verified=True
            )
        )

    if outcome.unavailable or outcome.failed:
        outcome.complete = False
        outcome.status = CONFIG_STATUS_ERROR
        outcome.reason = _error_reason(outcome, outcome.details)
        if len(outcome.unavailable) + len(outcome.failed) > 1:
            outcome.reason += " (and %d more)" % (
                len(outcome.unavailable) + len(outcome.failed) - 1
            )
    elif outcome.restored:
        outcome.status = CONFIG_STATUS_RESTORED
    else:
        outcome.status = CONFIG_STATUS_SKIPPED
        outcome.reason = (
            "configuration was left unchanged; all %d file(s) matched the "
            "backup or were kept by policy" % len(outcome.kept)
        )
    return outcome


__all__ = [
    "FILES_DIR_NAME",
    "MANIFEST_NAME",
    "locate_manifest",
    "restore_configs",
]
