# -*- coding: utf-8 -*-
"""V2 verification: relative path set, per-file bytes, count and total bytes.

Both sides are traversed iteratively with ``os.scandir``.  Reparse points are
recorded but never entered, matching ``robocopy /XJ``.  A permission error on
either side fails verification outright, so the caller never deletes source
data on an uncertain result (architecture 6.1/6.6).

Python 3.8 compatible (decision 0005).
"""

import os
import stat as stat_module
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from ...platform import is_reparse_stat
from ..scan.cancel import is_cancelled
from .models import MigrationCancelled, SizeMismatch, VerifyResult

MAX_LISTED_PATHS = 50


@dataclass
class _Snapshot:
    root: str
    is_file: bool
    files: Dict[str, Tuple[str, int]] = field(default_factory=dict)
    file_count: int = 0
    total_bytes: int = 0
    reparse_paths: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


def _display_rel(rel):
    # type: (str) -> str
    return rel.replace("\\", "/")


def _key(rel):
    # type: (str) -> str
    return os.path.normcase(_display_rel(rel))


def _check_cancelled(token):
    # type: (object) -> None
    if is_cancelled(token):
        raise MigrationCancelled("migration cancelled during verification")


def _snapshot(root, cancel_token=None):
    # type: (str, object) -> _Snapshot
    snapshot = _Snapshot(root=root, is_file=False)
    try:
        root_info = os.lstat(root)
    except OSError as exc:
        snapshot.errors.append("%s: %s" % (root, exc))
        return snapshot
    if is_reparse_stat(root_info):
        snapshot.errors.append("%s is a reparse point" % root)
        return snapshot
    if not stat_module.S_ISDIR(root_info.st_mode):
        if not stat_module.S_ISREG(root_info.st_mode):
            snapshot.errors.append("%s is not a regular file" % root)
            return snapshot
        # A file root is compared as a whole: its identity is the root
        # itself, not its basename, which may legitimately differ on the
        # staging side (robocopy copies the file into a directory).
        snapshot.is_file = True
        snapshot.files[_key(".")] = (".", root_info.st_size)
        snapshot.file_count = 1
        snapshot.total_bytes = root_info.st_size
        return snapshot

    stack = [(root, "")]
    while stack:
        _check_cancelled(cancel_token)
        directory, prefix = stack.pop()
        try:
            with os.scandir(directory) as entries:
                children = list(entries)
        except OSError as exc:
            snapshot.errors.append("%s: %s" % (directory, exc))
            continue
        for entry in children:
            _check_cancelled(cancel_token)
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                snapshot.errors.append("%s: %s" % (entry.path, exc))
                continue
            rel = prefix + "/" + entry.name if prefix else entry.name
            if is_reparse_stat(info):
                snapshot.reparse_paths.append(_display_rel(rel))
                continue
            if stat_module.S_ISDIR(info.st_mode):
                stack.append((entry.path, rel))
            elif stat_module.S_ISREG(info.st_mode):
                snapshot.files[_key(rel)] = (_display_rel(rel), info.st_size)
                snapshot.file_count += 1
                snapshot.total_bytes += info.st_size
    return snapshot


def _limit(items):
    # type: (List[str]) -> List[str]
    return items[:MAX_LISTED_PATHS]


def verify_trees(source_path, target_path, cancel_token=None):
    # type: (str, str, object) -> VerifyResult
    """Compare a source tree/file with its staging counterpart."""
    source = _snapshot(source_path, cancel_token)
    _check_cancelled(cancel_token)
    target = _snapshot(target_path, cancel_token)

    result = VerifyResult(ok=False)
    result.source_file_count = source.file_count
    result.target_file_count = target.file_count
    result.source_bytes = source.total_bytes
    result.target_bytes = target.total_bytes
    result.errors = source.errors + target.errors

    missing_keys = sorted(set(source.files) - set(target.files))
    extra_keys = sorted(set(target.files) - set(source.files))
    common_keys = set(source.files) & set(target.files)

    mismatches = []  # type: List[SizeMismatch]
    for key in sorted(common_keys):
        source_rel, source_size = source.files[key]
        _target_rel, target_size = target.files[key]
        if source_size != target_size:
            mismatches.append(
                SizeMismatch(
                    rel_path=source_rel,
                    source_bytes=source_size,
                    target_bytes=target_size,
                )
            )

    result.missing_count = len(missing_keys)
    result.extra_count = len(extra_keys)
    result.size_mismatch_count = len(mismatches)
    result.missing = _limit([source.files[key][0] for key in missing_keys])
    result.extra = _limit([target.files[key][0] for key in extra_keys])
    result.size_mismatches = mismatches[:MAX_LISTED_PATHS]
    result.reparse_paths = sorted(
        set(source.reparse_paths) | set(target.reparse_paths)
    )

    reasons = []  # type: List[str]
    if result.errors:
        reasons.append("%d access/read error(s)" % len(result.errors))
    if result.missing_count or result.extra_count:
        reasons.append(
            "path set differs (missing=%d, extra=%d)"
            % (result.missing_count, result.extra_count)
        )
    if result.size_mismatch_count:
        reasons.append(
            "%d file(s) differ in size" % result.size_mismatch_count
        )
    if source.file_count != target.file_count:
        reasons.append(
            "file count differs (source=%d, target=%d)"
            % (source.file_count, target.file_count)
        )
    if source.total_bytes != target.total_bytes:
        reasons.append(
            "total bytes differ (source=%d, target=%d)"
            % (source.total_bytes, target.total_bytes)
        )

    result.ok = not reasons
    result.reason = "verification passed" if result.ok else "; ".join(reasons)
    return result


__all__ = ["MAX_LISTED_PATHS", "verify_trees"]
