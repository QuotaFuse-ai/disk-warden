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
* D backup missing or corrupt -> mark the file unavailable and list it; the
  data rollback still completes (config status is reported separately).

A config failure never deletes an existing file and never changes the data
outcome.  All writes go through ``atomic_write_bytes``.

Python 3.8 compatible (decision 0005).
"""

import hashlib
import os
from typing import Dict, Optional, Tuple

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
    CONFIG_STATUS_FAILED,
    CONFIG_STATUS_KEPT,
    CONFIG_STATUS_NOT_REQUIRED,
    CONFIG_STATUS_PARTIAL,
    CONFIG_STATUS_RESTORED,
    CONFIG_STATUS_UNAVAILABLE,
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


def _load_manifest(backup_dir):
    # type: (str) -> Tuple[Optional[Dict[str, object]], str]
    manifest_path = os.path.join(backup_dir, MANIFEST_NAME)
    try:
        payload = read_json_file(manifest_path)
    except FileNotFoundError:
        return None, "backup manifest is missing"
    except (StoreError, OSError) as exc:
        return None, "backup manifest is unreadable: %s" % exc
    if not isinstance(payload, dict):
        return None, "backup manifest has an unexpected shape"
    return payload, ""


def _restore_file(target, payload):
    # type: (str, bytes) -> None
    atomic_write_bytes(target, payload)


def restore_configs(
    backup_dir, choice=CONFIG_KEEP, expected_tool_id=None, expected_job_id=None
):
    # type: (Optional[str], str, Optional[str], Optional[str]) -> ConfigRestoreOutcome
    """Apply AC-ROL-06 A/B/C/D to every file listed in the backup manifest."""
    outcome = ConfigRestoreOutcome()
    if choice not in CONFIG_CHOICES:
        choice = CONFIG_KEEP

    if not backup_dir:
        outcome.status = CONFIG_STATUS_UNAVAILABLE
        outcome.complete = False
        outcome.reason = "no configuration backup was recorded for this job"
        return outcome

    backup_dir = os.path.abspath(os.path.normpath(backup_dir))
    manifest, error = _load_manifest(backup_dir)
    if manifest is None:
        outcome.status = CONFIG_STATUS_UNAVAILABLE
        outcome.complete = False
        outcome.reason = error
        return outcome

    if expected_tool_id and manifest.get("tool_id") not in (None, "", expected_tool_id):
        outcome.status = CONFIG_STATUS_UNAVAILABLE
        outcome.complete = False
        outcome.reason = "backup manifest belongs to another tool"
        return outcome
    if expected_job_id and manifest.get("job_id") not in (None, "", expected_job_id):
        outcome.status = CONFIG_STATUS_UNAVAILABLE
        outcome.complete = False
        outcome.reason = "backup manifest belongs to another job"
        return outcome

    source_root = manifest.get("source_root")
    if not isinstance(source_root, str) or not source_root.strip():
        outcome.status = CONFIG_STATUS_UNAVAILABLE
        outcome.complete = False
        outcome.reason = "backup manifest has no source root"
        return outcome
    source_root = os.path.abspath(os.path.normpath(source_root))

    entries = manifest.get("entries")
    if not isinstance(entries, list):
        outcome.status = CONFIG_STATUS_UNAVAILABLE
        outcome.complete = False
        outcome.reason = "backup manifest has no entry list"
        return outcome

    if not entries:
        outcome.status = CONFIG_STATUS_NOT_REQUIRED
        outcome.reason = "the backup contains no configuration files"
        return outcome

    for raw in entries:
        if not isinstance(raw, dict):
            outcome.complete = False
            outcome.unavailable.append("")
            outcome.details.append(
                {"path": "", "action": CONFIG_ACTION_BACKUP_CORRUPT}
            )
            continue
        rel = _safe_rel_path(raw.get("rel_path"))
        if rel is None:
            outcome.complete = False
            outcome.unavailable.append(str(raw.get("rel_path") or ""))
            outcome.details.append(
                {
                    "path": str(raw.get("rel_path") or ""),
                    "action": CONFIG_ACTION_BACKUP_CORRUPT,
                    "reason": "unsafe relative path",
                }
            )
            continue
        target = _target_path(source_root, rel)
        if target is None:
            outcome.complete = False
            outcome.unavailable.append(rel)
            outcome.details.append(
                {
                    "path": rel,
                    "action": CONFIG_ACTION_BACKUP_CORRUPT,
                    "reason": "path escapes the recorded source root",
                }
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
                {"path": rel, "action": CONFIG_ACTION_BACKUP_MISSING}
            )
            continue
        except OSError as exc:
            outcome.complete = False
            outcome.unavailable.append(rel)
            outcome.details.append(
                {
                    "path": rel,
                    "action": CONFIG_ACTION_BACKUP_CORRUPT,
                    "reason": str(exc),
                }
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
                {
                    "path": rel,
                    "action": CONFIG_ACTION_BACKUP_CORRUPT,
                    "reason": "checksum or size mismatch",
                }
            )
            continue

        try:
            with open(backup_file, "rb") as handle:
                payload = handle.read()
        except OSError as exc:
            outcome.complete = False
            outcome.unavailable.append(rel)
            outcome.details.append(
                {
                    "path": rel,
                    "action": CONFIG_ACTION_BACKUP_CORRUPT,
                    "reason": str(exc),
                }
            )
            continue

        if os.path.isfile(target):
            try:
                current_digest, _current_size = _sha256_file(target)
            except OSError as exc:
                outcome.complete = False
                outcome.unavailable.append(rel)
                outcome.details.append(
                    {
                        "path": rel,
                        "action": CONFIG_ACTION_BACKUP_CORRUPT,
                        "reason": "cannot read the current file: %s" % exc,
                    }
                )
                continue
            if current_digest == actual_digest:
                outcome.kept.append(rel)
                outcome.details.append(
                    {"path": rel, "action": CONFIG_ACTION_IDENTICAL}
                )
                continue
            if choice != CONFIG_RESTORE:
                outcome.kept.append(rel)
                outcome.details.append(
                    {"path": rel, "action": CONFIG_ACTION_KEPT_USER}
                )
                continue
            action = CONFIG_ACTION_RESTORED_USER_CHOICE
        else:
            action = CONFIG_ACTION_RESTORED_MISSING

        try:
            _restore_file(target, payload)
        except (StoreError, OSError) as exc:
            outcome.failed.append(rel)
            outcome.details.append(
                {
                    "path": rel,
                    "action": CONFIG_ACTION_RESTORE_FAILED,
                    "reason": str(exc),
                }
            )
            continue
        outcome.restored.append(rel)
        outcome.details.append({"path": rel, "action": action})

    if outcome.unavailable or outcome.failed:
        outcome.complete = False
        if outcome.restored or outcome.kept:
            outcome.status = CONFIG_STATUS_PARTIAL
        else:
            outcome.status = CONFIG_STATUS_FAILED
        outcome.reason = "some configuration files could not be restored"
    elif outcome.restored:
        outcome.status = CONFIG_STATUS_RESTORED
    else:
        outcome.status = CONFIG_STATUS_KEPT
    return outcome


__all__ = [
    "FILES_DIR_NAME",
    "MANIFEST_NAME",
    "restore_configs",
]
