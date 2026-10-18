# -*- coding: utf-8 -*-
"""S5/S6: promote the verified staging copy and swap the source aside.

Both renames are same-volume ``os.rename`` calls, so each either happens or
does not (architecture 6.2).  No function here deletes source data: the source
is renamed to ``.dwtombstone-<job8>`` and stays there until the link has been
verified.  Every function is idempotent and inspects the filesystem before
acting, so a retry after a crash never renames twice.

Python 3.8 compatible (decision 0005).
"""

import os

from ...platform import LinkError
from . import marker, paths
from .cleanup import UnsafeCleanupError, remove_owned_tree, remove_tree


class SwapError(RuntimeError):
    """Raised when a swap step cannot run safely; nothing was deleted."""


def discard_staging(staging_path, job_id):
    # type: (str, str) -> list
    """Remove ``<tool_dir>/.dw-partial-<job8>``; returns error strings."""
    staging_parent = os.path.dirname(os.path.abspath(staging_path))
    tool_dir = os.path.dirname(staging_parent)
    try:
        return remove_tree(
            staging_parent,
            tool_dir,
            lambda name: paths.is_partial_dir_name(name, job_id),
        )
    except (UnsafeCleanupError, OSError) as exc:
        return [str(exc)]


def discard_target(final_path, job_id):
    # type: (str, str) -> list
    """Remove a promoted final target only when this job's marker owns it."""
    final_path = os.path.abspath(final_path)
    try:
        return remove_owned_tree(final_path, os.path.dirname(final_path), job_id)
    except (UnsafeCleanupError, OSError) as exc:
        return [str(exc)]


def ensure_target_ready(ops, staging_path, final_path, job_id):
    # type: (object, str, str, str) -> tuple
    """S5: atomically rename the verified staging directory to the target.

    Returns ``(promoted_now, cleanup_errors)``.  ``promoted_now`` is False when
    the target already carries this job's marker (a previous run promoted it).
    Raises ``SwapError`` without deleting anything when the state is ambiguous.
    """
    staging_path = os.path.abspath(staging_path)
    final_path = os.path.abspath(final_path)
    staging_parent = os.path.dirname(staging_path)
    tool_dir = os.path.dirname(final_path)

    if not paths.same_path(os.path.dirname(staging_parent), tool_dir):
        raise SwapError("staging and final do not share a parent directory")
    if not paths.is_partial_dir_name(os.path.basename(staging_parent), job_id):
        raise SwapError("staging directory is not owned by this job")
    if not paths.same_path(
        os.path.basename(staging_path), os.path.basename(final_path)
    ):
        raise SwapError("staging leaf does not match the source leaf")

    cleanup_errors = []  # type: list
    if ops.exists(final_path):
        if ops.is_link(final_path) or not ops.is_dir(final_path):
            raise SwapError("final target is not a plain directory")
        if not marker.owns_directory(final_path, job_id):
            raise SwapError(
                "final target exists and does not carry this job's marker"
            )
        if ops.exists(staging_path):
            cleanup_errors.extend(discard_staging(staging_path, job_id))
        return False, cleanup_errors

    if not ops.exists(staging_path):
        raise SwapError("staging is missing and the final target does not exist")
    if ops.is_link(staging_path) or not ops.is_dir(staging_path):
        raise SwapError("staging is not a plain directory")

    payload = marker.read_owner_marker(staging_path, job_id)
    if payload is None:
        raise SwapError("staging has no ownership marker for this job")
    recorded = payload.get("final_path")
    if recorded and not paths.same_path(str(recorded), final_path):
        raise SwapError(
            "staging marker records %r, expected %r" % (recorded, final_path)
        )
    if not ops.same_volume(staging_path, final_path):
        raise SwapError("staging and final are on different volumes")

    try:
        ops.rename(staging_path, final_path)
    except (LinkError, OSError) as exc:
        raise SwapError("cannot promote staging: %s" % exc)
    return True, cleanup_errors


def ensure_source_renamed(ops, source_path, tombstone_path, job_id):
    # type: (object, str, str, str) -> bool
    """S6: atomically rename the source to its tombstone.

    Returns True when the rename happened in this call and False when a
    previous run already renamed it.  Raises ``SwapError`` when both paths
    exist (ambiguous) or when either is not a plain directory.
    """
    source_path = os.path.abspath(source_path)
    tombstone_path = os.path.abspath(tombstone_path)
    if not paths.is_tombstone_name(os.path.basename(tombstone_path), job_id):
        raise SwapError("tombstone name is not owned by this job")
    if not paths.same_path(
        os.path.dirname(tombstone_path), os.path.dirname(source_path)
    ):
        raise SwapError("tombstone must live beside the source")

    source_exists = ops.exists(source_path)
    tombstone_exists = ops.exists(tombstone_path)

    if source_exists:
        if ops.is_link(source_path) or not ops.is_dir(source_path):
            raise SwapError("source is not a plain directory")
        if tombstone_exists:
            raise SwapError(
                "source and tombstone both exist; refusing to guess: %s"
                % tombstone_path
            )
        if not ops.same_volume(source_path, tombstone_path):
            raise SwapError("source and tombstone are on different volumes")
        try:
            ops.rename(source_path, tombstone_path)
        except (LinkError, OSError) as exc:
            raise SwapError("cannot rename source to tombstone: %s" % exc)
        return True

    if tombstone_exists:
        if ops.is_link(tombstone_path) or not ops.is_dir(tombstone_path):
            raise SwapError("tombstone is not a plain directory")
        return False
    raise SwapError("source and tombstone are both missing")


def restore_source(ops, source_path, tombstone_path, job_id):
    # type: (object, str, str, str) -> bool
    """Rename the tombstone back to the source path (idempotent).

    Returns True when the rename happened, False when the source is already a
    real directory.  Never overwrites an occupied source path.
    """
    source_path = os.path.abspath(source_path)
    tombstone_path = os.path.abspath(tombstone_path)
    if not paths.is_tombstone_name(os.path.basename(tombstone_path), job_id):
        raise SwapError("tombstone name is not owned by this job")
    if not paths.same_path(
        os.path.dirname(tombstone_path), os.path.dirname(source_path)
    ):
        raise SwapError("tombstone must live beside the source")

    tombstone_exists = ops.exists(tombstone_path)
    source_exists = ops.exists(source_path)

    if not tombstone_exists:
        if source_exists and ops.is_dir(source_path) and not ops.is_link(source_path):
            return False
        raise SwapError("tombstone is missing; source cannot be restored")
    if source_exists:
        raise SwapError("source path is occupied; refusing to overwrite it")
    if ops.is_link(tombstone_path) or not ops.is_dir(tombstone_path):
        raise SwapError("tombstone is not a plain directory")
    if not ops.same_volume(source_path, tombstone_path):
        raise SwapError("source and tombstone are on different volumes")
    try:
        ops.rename(tombstone_path, source_path)
    except (LinkError, OSError) as exc:
        raise SwapError("cannot restore source from tombstone: %s" % exc)
    return True


__all__ = [
    "SwapError",
    "discard_staging",
    "discard_target",
    "ensure_source_renamed",
    "ensure_target_ready",
    "restore_source",
]
