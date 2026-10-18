# -*- coding: utf-8 -*-
"""Ownership marker for a job's final target directory (architecture 6.5).

The marker is written into staging **after** verification (T09 limitation #7),
so it never changes the counts verification compared.  It is the only proof a
later step needs before deleting a promoted target that carries the source's
basename, which is not itself a job-prefixed name.

Python 3.8 compatible (decision 0005).
"""

import datetime
import json
import os

from . import paths
from .paths import PathValidationError

SCHEMA_VERSION = 1
_TMP_SUFFIX = ".tmp"


def marker_path(directory, job_id):
    # type: (str, str) -> str
    return os.path.join(directory, paths.owner_marker_name(job_id))


def write_owner_marker(
    directory,
    job_id,
    tool_id,
    source_path,
    final_path,
    file_count=0,
    total_bytes=0,
    now=None,
):
    # type: (str, str, str, str, str, int, int, object) -> str
    """Atomically write ``.dw-owner-<job8>.json`` and return its path."""
    moment = now or datetime.datetime.now()
    payload = {
        "schema_version": SCHEMA_VERSION,
        "job_id": job_id,
        "tool_id": tool_id,
        "source_path": source_path,
        "final_path": final_path,
        "file_count": int(file_count),
        "total_bytes": int(total_bytes),
        "created_at": moment.replace(microsecond=0).isoformat(),
    }
    destination = marker_path(directory, job_id)
    temporary = destination + _TMP_SUFFIX
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, destination)
    return destination


def read_owner_marker(directory, job_id=None):
    # type: (str, str) -> dict
    """Read the marker owned by ``job_id`` (or any job when omitted).

    Returns ``None`` when the marker is absent, unreadable or malformed.
    """
    if job_id is not None:
        try:
            candidates = [marker_path(directory, job_id)]
        except PathValidationError:
            return None
    else:
        try:
            with os.scandir(directory) as entries:
                candidates = sorted(
                    entry.path
                    for entry in entries
                    if entry.is_file(follow_symlinks=False)
                    and paths.is_owner_marker_name(entry.name)
                )
        except OSError:
            return None

    for candidate in candidates:
        try:
            with open(candidate, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            return payload
    return None


def owns_directory(directory, job_id):
    # type: (str, str) -> bool
    """True only when ``directory`` carries this job's marker."""
    if not os.path.isdir(directory):
        return False
    payload = read_owner_marker(directory, job_id)
    if payload is None:
        return False
    if payload.get("job_id") != job_id:
        return False
    recorded = payload.get("final_path")
    if recorded:
        expected = os.path.normcase(
            os.path.normpath(os.path.abspath(directory))
        )
        actual = os.path.normcase(
            os.path.normpath(os.path.abspath(str(recorded)))
        )
        if actual != expected:
            return False
    return True


__all__ = [
    "SCHEMA_VERSION",
    "marker_path",
    "owns_directory",
    "read_owner_marker",
    "write_owner_marker",
]
