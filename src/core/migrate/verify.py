# -*- coding: utf-8 -*-
"""V2 verification: relative path set, per-file size **and content**.

Size alone is not evidence: a copy error or a swapped file that keeps the
same byte count passes a size-only check, and rollback deletes the target
copy once verification succeeds -- that was the only silent data-loss path
in the product (T14 F-02, T26/R-02).  Every file therefore carries a content
fingerprint:

* files up to :data:`FULL_HASH_LIMIT` are hashed whole with SHA-256;
* larger files record ``(size, fingerprint)`` over the first block, the last
  block and :data:`SAMPLE_BLOCK_COUNT` evenly spaced blocks in between.

Sampling cannot prove two large files are identical, but it detects the
realistic failure modes (truncation, zero-filled tails, partial rewrites,
swapped files) at a bounded, predictable cost: at most
``(SAMPLE_BLOCK_COUNT + 2) * SAMPLE_BLOCK_BYTES`` read per large file instead
of the whole file.

Both sides are traversed iteratively with ``os.scandir``.  Reparse points are
recorded but never entered, matching ``robocopy /XJ``.  A permission or read
error on either side fails verification outright, so the caller never deletes
source data on an uncertain result (architecture 6.1/6.6).

Python 3.8 compatible (decision 0005).
"""

import hashlib
import os
import stat as stat_module
import struct
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from ...platform import is_reparse_stat
from ..scan.cancel import is_cancelled
from .models import (
    ContentMismatch,
    MigrationCancelled,
    SizeMismatch,
    VerifyResult,
)

MAX_LISTED_PATHS = 50

# Files up to this size are hashed in full; larger ones are sampled.
FULL_HASH_LIMIT = 4 * 1024 * 1024
# Blocks read from the head, the tail and evenly spaced offsets of a large
# file.  Eight 64 KiB probes read ~640 KiB per file regardless of its size.
SAMPLE_BLOCK_BYTES = 64 * 1024
SAMPLE_BLOCK_COUNT = 8
# Fingerprint prefixes keep full and sampled digests distinguishable.
FULL_DIGEST_PREFIX = "sha256:"
SAMPLED_DIGEST_PREFIX = "sha256-sampled:"
_READ_CHUNK_BYTES = 1024 * 1024


@dataclass
class _Snapshot:
    root: str
    is_file: bool
    files: Dict[str, Tuple[str, int, str]] = field(default_factory=dict)
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


def _full_digest(path):
    # type: (str) -> str
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_READ_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return FULL_DIGEST_PREFIX + digest.hexdigest()


def _sampled_digest(path, size):
    # type: (str, int) -> str
    """SHA-256 over head, tail and evenly spaced blocks of a large file.

    The offsets are part of the digest so two files cannot collide merely by
    permuting their blocks.  ``size`` pins the sample layout, so both sides
    of one comparison always probe the same offsets.
    """
    block = min(SAMPLE_BLOCK_BYTES, size)
    last = size - block
    offsets = {0, last}
    for index in range(1, SAMPLE_BLOCK_COUNT + 1):
        offsets.add((last * index) // (SAMPLE_BLOCK_COUNT + 1))
    digest = hashlib.sha256()
    digest.update(struct.pack("<QQ", size, block))
    with open(path, "rb") as handle:
        for offset in sorted(offsets):
            handle.seek(offset)
            payload = handle.read(block)
            digest.update(struct.pack("<Q", offset))
            digest.update(payload)
    return SAMPLED_DIGEST_PREFIX + digest.hexdigest()


def content_fingerprint(path, size):
    # type: (str, int) -> str
    """Return the content fingerprint of one regular file.

    Raises ``OSError`` when the file cannot be read; callers record that as a
    verification error instead of assuming the content matches.
    """
    if size <= FULL_HASH_LIMIT:
        return _full_digest(path)
    return _sampled_digest(path, size)


def _snapshot(root, cancel_token=None, ignore=()):
    # type: (str, object, tuple) -> _Snapshot
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
        if _key(".") in ignore:
            return snapshot
        # A file root is compared as a whole: its identity is the root
        # itself, not its basename, which may legitimately differ on the
        # staging side (robocopy copies the file into a directory).
        snapshot.is_file = True
        try:
            fingerprint = content_fingerprint(root, root_info.st_size)
        except OSError as exc:
            snapshot.errors.append("%s: %s" % (root, exc))
            fingerprint = ""
        snapshot.files[_key(".")] = (".", root_info.st_size, fingerprint)
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
            if _key(rel) in ignore:
                continue
            if is_reparse_stat(info):
                snapshot.reparse_paths.append(_display_rel(rel))
                continue
            if stat_module.S_ISDIR(info.st_mode):
                stack.append((entry.path, rel))
            elif stat_module.S_ISREG(info.st_mode):
                try:
                    fingerprint = content_fingerprint(entry.path, info.st_size)
                except OSError as exc:
                    snapshot.errors.append("%s: %s" % (entry.path, exc))
                    fingerprint = ""
                snapshot.files[_key(rel)] = (
                    _display_rel(rel),
                    info.st_size,
                    fingerprint,
                )
                snapshot.file_count += 1
                snapshot.total_bytes += info.st_size
    return snapshot


def _limit(items):
    # type: (List[str]) -> List[str]
    return items[:MAX_LISTED_PATHS]


def verify_trees(source_path, target_path, cancel_token=None, ignore_paths=()):
    # type: (str, str, object, tuple) -> VerifyResult
    """Compare a source tree/file with its staging counterpart.

    ``ignore_paths`` lists relative paths (such as the ownership marker) that
    are skipped on both sides; anything else must match in path, size and
    content.
    """
    ignore = frozenset(_key(rel) for rel in ignore_paths or ())
    source = _snapshot(source_path, cancel_token, ignore)
    _check_cancelled(cancel_token)
    target = _snapshot(target_path, cancel_token, ignore)

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
    content_mismatches = []  # type: List[ContentMismatch]
    for key in sorted(common_keys):
        source_rel, source_size, source_digest = source.files[key]
        _target_rel, target_size, target_digest = target.files[key]
        if source_size != target_size:
            mismatches.append(
                SizeMismatch(
                    rel_path=source_rel,
                    source_bytes=source_size,
                    target_bytes=target_size,
                )
            )
        elif not source_digest or not target_digest:
            # No fingerprint means the content was not proven equal; the
            # read error itself already fails the result.
            content_mismatches.append(ContentMismatch(rel_path=source_rel))
        elif source_digest != target_digest:
            content_mismatches.append(ContentMismatch(rel_path=source_rel))

    result.missing_count = len(missing_keys)
    result.extra_count = len(extra_keys)
    result.size_mismatch_count = len(mismatches)
    result.content_mismatch_count = len(content_mismatches)
    result.missing = _limit([source.files[key][0] for key in missing_keys])
    result.extra = _limit([target.files[key][0] for key in extra_keys])
    result.size_mismatches = mismatches[:MAX_LISTED_PATHS]
    result.content_mismatches = content_mismatches[:MAX_LISTED_PATHS]
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
    if result.content_mismatch_count:
        reasons.append(
            "%d file(s) differ in content" % result.content_mismatch_count
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


__all__ = [
    "FULL_HASH_LIMIT",
    "MAX_LISTED_PATHS",
    "SAMPLE_BLOCK_BYTES",
    "SAMPLE_BLOCK_COUNT",
    "content_fingerprint",
    "verify_trees",
]
