# -*- coding: utf-8 -*-
"""Narrowly scoped deletion for job-owned temporary directories.

Only directories whose name embeds this job's 8-hex prefix may be removed, and
reparse points are removed as links without ever entering their target
(architecture 6.1/6.5).  Source data is never passed to this module.

Python 3.8 compatible (decision 0005).
"""

import os
import stat

from ...platform import is_reparse_stat
from . import marker, paths


class UnsafeCleanupError(RuntimeError):
    """Raised when a path fails the ownership checks; nothing is deleted."""


def _unlink(path):
    # type: (str) -> None
    try:
        os.unlink(path)
    except PermissionError:
        os.chmod(path, stat.S_IWRITE)
        os.unlink(path)


def _remove_link(path, info):
    # type: (str, os.stat_result) -> None
    if stat.S_ISDIR(info.st_mode):
        os.rmdir(path)
    else:
        _unlink(path)


def remove_tree(path, expected_parent, name_check):
    # type: (str, str, object) -> list
    """Delete ``path`` after proving it is a job-owned directory.

    ``name_check`` receives the directory name and returns True only for this
    job's naming pattern.  Returns a list of error strings; deletion continues
    past individual failures so one locked file cannot strand the rest.
    """
    absolute = os.path.abspath(path)
    if os.path.dirname(absolute) != os.path.abspath(expected_parent):
        raise UnsafeCleanupError(
            "refusing to delete outside the expected parent: %s" % absolute
        )
    if not name_check(os.path.basename(absolute)):
        raise UnsafeCleanupError(
            "refusing to delete a directory not owned by this job: %s" % absolute
        )

    try:
        root_info = os.lstat(absolute)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise UnsafeCleanupError("cannot inspect %s: %s" % (absolute, exc))

    if not stat.S_ISDIR(root_info.st_mode):
        raise UnsafeCleanupError("%s is not a directory" % absolute)
    if is_reparse_stat(root_info):
        raise UnsafeCleanupError("%s is a reparse point" % absolute)

    errors = []  # type: list
    stack = [(absolute, False)]
    while stack:
        current, visited = stack.pop()
        if visited:
            try:
                os.rmdir(current)
            except OSError as exc:
                errors.append("%s: %s" % (current, exc))
            continue

        try:
            with os.scandir(current) as entries:
                children = list(entries)
        except OSError as exc:
            errors.append("%s: %s" % (current, exc))
            continue

        stack.append((current, True))
        for entry in children:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                errors.append("%s: %s" % (entry.path, exc))
                continue
            if is_reparse_stat(info):
                try:
                    _remove_link(entry.path, info)
                except OSError as exc:
                    errors.append("%s: %s" % (entry.path, exc))
            elif stat.S_ISDIR(info.st_mode):
                stack.append((entry.path, False))
            else:
                try:
                    _unlink(entry.path)
                except OSError as exc:
                    errors.append("%s: %s" % (entry.path, exc))
    return errors


def remove_owned_tree(path, expected_parent, job_id):
    # type: (str, str, str) -> list
    """Delete a promoted target only when this job's marker proves ownership.

    The target carries the source's basename rather than a job-prefixed name,
    so the marker is the ownership proof; everything else follows the same
    checks as :func:`remove_tree` (architecture 6.5).
    """
    absolute = os.path.abspath(path)
    if os.path.dirname(absolute) != os.path.abspath(expected_parent):
        raise UnsafeCleanupError(
            "refusing to delete outside the expected parent: %s" % absolute
        )
    try:
        root_info = os.lstat(absolute)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise UnsafeCleanupError("cannot inspect %s: %s" % (absolute, exc))
    if not stat.S_ISDIR(root_info.st_mode):
        raise UnsafeCleanupError("%s is not a directory" % absolute)
    if is_reparse_stat(root_info):
        raise UnsafeCleanupError("%s is a reparse point" % absolute)
    if not marker.owns_directory(absolute, job_id):
        raise UnsafeCleanupError(
            "refusing to delete a target without this job's ownership marker: %s"
            % absolute
        )
    return remove_tree(absolute, expected_parent, lambda name: True)


def remove_tombstone(path, job_id):
    # type: (str, str) -> list
    """Delete the job's tombstone after the link has been verified.

    Refuses any name that does not embed this job's 8-hex prefix.
    """
    absolute = os.path.abspath(path)
    parent = os.path.dirname(absolute)
    name = os.path.basename(absolute)
    if not paths.is_tombstone_name(name, job_id):
        raise UnsafeCleanupError(
            "refusing to delete a tombstone not owned by this job: %s" % absolute
        )
    try:
        info = os.lstat(absolute)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise UnsafeCleanupError("cannot inspect %s: %s" % (absolute, exc))

    if is_reparse_stat(info):
        raise UnsafeCleanupError("%s is a reparse point" % absolute)
    if stat.S_ISDIR(info.st_mode):
        return remove_tree(
            absolute,
            parent,
            lambda child: paths.is_tombstone_name(child, job_id),
        )
    if stat.S_ISREG(info.st_mode):
        _unlink(absolute)
        return []
    raise UnsafeCleanupError("%s is not a regular file or directory" % absolute)


__all__ = [
    "UnsafeCleanupError",
    "remove_owned_tree",
    "remove_tombstone",
    "remove_tree",
]
