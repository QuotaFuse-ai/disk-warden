# -*- coding: utf-8 -*-
"""Naming rules for migration jobs (architecture 6.2).

Every temporary path embeds the job's 8-hex prefix so cleanup and recovery can
prove a directory belongs to this job instead of deleting by wildcard.

Python 3.8 compatible (decision 0005).
"""

import datetime
import os
import re
import uuid

JOB_ID_RE = re.compile(r"^[0-9a-f]{8,32}$")
PARTIAL_DIR_RE = re.compile(r"^\.dw-partial-([0-9a-f]{8})$")
TOMBSTONE_DIR_RE = re.compile(r"^\.dwtombstone-([0-9a-f]{8})$")
OWNER_MARKER_RE = re.compile(r"^\.dw-owner-([0-9a-f]{8})\.json$")
BACKUP_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}_.+_([0-9a-f]{8})$")
TOOL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class PathValidationError(ValueError):
    """Raised when a job id, tool id or naming pattern is not trustworthy."""


def new_job_id():
    # type: () -> str
    return uuid.uuid4().hex


def job_prefix(job_id):
    # type: (str) -> str
    value = (job_id or "").lower()
    if not JOB_ID_RE.match(value):
        raise PathValidationError("invalid job id: %r" % (job_id,))
    return value[:8]


def validate_tool_id(tool_id):
    # type: (str) -> str
    if not tool_id or not TOOL_ID_RE.match(tool_id) or tool_id in (".", ".."):
        raise PathValidationError("invalid tool id: %r" % (tool_id,))
    return tool_id


def source_basename(source_path):
    # type: (str) -> str
    """Leaf name of the migrated item; drive roots have no leaf and are invalid."""
    name = os.path.basename(os.path.normpath(source_path))
    if not name or name in (".", ".."):
        raise PathValidationError(
            "cannot derive a target name from %r" % (source_path,)
        )
    return name


def final_path(target_root, tool_id, source_path):
    # type: (str, str, str) -> str
    return os.path.join(target_root, validate_tool_id(tool_id), source_basename(source_path))


def staging_parent(target_root, tool_id, job_id):
    # type: (str, str, str) -> str
    name = ".dw-partial-%s" % job_prefix(job_id)
    return os.path.join(target_root, validate_tool_id(tool_id), name)


def staging_path(target_root, tool_id, job_id, source_path):
    # type: (str, str, str, str) -> str
    return os.path.join(
        staging_parent(target_root, tool_id, job_id),
        source_basename(source_path),
    )


def backup_dir(backup_root, tool_id, job_id, now=None):
    # type: (str, str, str, object) -> str
    moment = now or datetime.datetime.now()
    name = "%s_%s_%s" % (
        moment.strftime("%Y-%m-%d_%H%M%S"),
        validate_tool_id(tool_id),
        job_prefix(job_id),
    )
    return os.path.join(backup_root, name)


def tombstone_path(source_path, job_id):
    # type: (str, str) -> str
    """Same-parent slot the source is atomically renamed to before linking."""
    parent = os.path.dirname(os.path.abspath(os.path.normpath(source_path)))
    return os.path.join(parent, ".dwtombstone-%s" % job_prefix(job_id))


def owner_marker_name(job_id):
    # type: (str) -> str
    return ".dw-owner-%s.json" % job_prefix(job_id)


def is_tombstone_name(name, job_id):
    # type: (str, str) -> bool
    match = TOMBSTONE_DIR_RE.match(name or "")
    return bool(match and match.group(1) == job_prefix(job_id))


def is_owner_marker_name(name):
    # type: (str) -> bool
    return bool(OWNER_MARKER_RE.match(name or ""))


def is_partial_dir_name(name, job_id):
    # type: (str, str) -> bool
    match = PARTIAL_DIR_RE.match(name or "")
    return bool(match and match.group(1) == job_prefix(job_id))


def is_backup_dir_name(name, job_id):
    # type: (str, str) -> bool
    match = BACKUP_DIR_RE.match(name or "")
    return bool(match and match.group(1) == job_prefix(job_id))


def same_path(left, right):
    # type: (str, str) -> bool
    left_norm = os.path.normpath(os.path.abspath(left))
    right_norm = os.path.normpath(os.path.abspath(right))
    if os.name == "nt":
        return left_norm.lower() == right_norm.lower()
    return left_norm == right_norm


def is_inside(candidate, root):
    # type: (str, str) -> bool
    """True when ``candidate`` equals or lives below ``root``."""
    candidate_norm = os.path.normpath(os.path.abspath(candidate))
    root_norm = os.path.normpath(os.path.abspath(root))
    if os.name == "nt":
        candidate_norm = candidate_norm.lower()
        root_norm = root_norm.lower()
    if candidate_norm == root_norm:
        return True
    prefix = root_norm if root_norm.endswith(os.sep) else root_norm + os.sep
    return candidate_norm.startswith(prefix)


__all__ = [
    "BACKUP_DIR_RE",
    "JOB_ID_RE",
    "OWNER_MARKER_RE",
    "PARTIAL_DIR_RE",
    "PathValidationError",
    "TOMBSTONE_DIR_RE",
    "backup_dir",
    "final_path",
    "is_backup_dir_name",
    "is_inside",
    "is_owner_marker_name",
    "is_partial_dir_name",
    "is_tombstone_name",
    "job_prefix",
    "new_job_id",
    "owner_marker_name",
    "same_path",
    "source_basename",
    "staging_parent",
    "staging_path",
    "tombstone_path",
    "validate_tool_id",
]
