# -*- coding: utf-8 -*-
"""S7/S8: create the junction and prove it points at the new location.

Creation is idempotent: an existing link whose read-back target resolves to
the final path counts as success, while a link that points elsewhere is never
overwritten (architecture 6.5).  Verification reads the target back and
probes one known file through the link, because a junction can exist without
being traversable.

Python 3.8 compatible (decision 0005).
"""

import os
import stat as stat_module
from dataclasses import dataclass

from ...platform import is_reparse_stat, link_target_matches
from . import paths


class LinkStepError(RuntimeError):
    """Raised when a link cannot be created or must not be touched.

    ``mismatch`` marks states where the path is occupied by something this job
    does not own; callers must stop without deleting anything.
    """

    def __init__(self, message, target=None, mismatch=False):
        # type: (str, str, bool) -> None
        RuntimeError.__init__(self, message)
        self.target = target
        self.mismatch = mismatch


@dataclass
class LinkCheck:
    ok: bool
    target: str
    target_matches: bool
    probe_rel: str = ""
    probe_kind: str = ""
    reason: str = ""

    def to_dict(self):
        # type: () -> dict
        return {
            "ok": self.ok,
            "target": self.target,
            "target_matches": self.target_matches,
            "probe_rel": self.probe_rel,
            "probe_kind": self.probe_kind,
            "reason": self.reason,
        }


def ensure_link(ops, link_path, final_path):
    # type: (object, str, str) -> tuple
    """Create ``link_path`` -> ``final_path``; returns ``(created, target)``.

    An existing matching link returns ``(False, target)``.  An existing
    non-link or a link that points elsewhere raises ``LinkStepError`` with
    ``mismatch=True``; nothing is removed.
    """
    if ops.exists(link_path):
        if not ops.is_junction(link_path):
            raise LinkStepError(
                "source path exists and is not a junction: %s" % link_path,
                mismatch=True,
            )
        target = ops.read_link_target(link_path)
        if link_target_matches(target, final_path):
            return False, target
        raise LinkStepError(
            "junction %s points to %r, expected %r"
            % (link_path, target, final_path),
            target=target,
            mismatch=True,
        )

    if not ops.is_dir(final_path):
        raise LinkStepError("junction target is not a directory: %s" % final_path)
    try:
        ops.create_link(link_path, final_path)
    except OSError as exc:
        raise LinkStepError("cannot create junction: %s" % exc)
    target = ops.read_link_target(link_path)
    if not link_target_matches(target, final_path):
        raise LinkStepError(
            "junction read-back %r does not resolve to %r"
            % (target, final_path),
            target=target,
            mismatch=True,
        )
    return True, target


def find_probe(final_path):
    # type: (str) -> str
    """Return the relative path of the first regular file below ``final_path``.

    The ownership marker is skipped, and reparse points are never entered.
    Returns an empty string when the tree has no regular file.
    """
    stack = [("", final_path)]
    while stack:
        prefix, directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                children = sorted(entries, key=lambda entry: entry.name)
        except OSError:
            continue
        subdirectories = []
        for entry in children:
            rel = prefix + "/" + entry.name if prefix else entry.name
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if is_reparse_stat(info):
                continue
            if stat_module.S_ISDIR(info.st_mode):
                subdirectories.append((rel, entry.path))
            elif stat_module.S_ISREG(info.st_mode):
                if paths.is_owner_marker_name(entry.name):
                    continue
                return rel
        stack.extend(reversed(subdirectories))
    return ""


def _join_rel(root, rel):
    # type: (str, str) -> str
    return os.path.join(root, *rel.split("/"))


def verify_link(ops, link_path, final_path, probe_rel=None):
    # type: (object, str, str, str) -> LinkCheck
    """Read the target back and probe one file through the link."""
    target = ops.read_link_target(link_path)
    if not target:
        return LinkCheck(
            ok=False,
            target="",
            target_matches=False,
            reason="junction target cannot be read",
        )
    if not link_target_matches(target, final_path):
        return LinkCheck(
            ok=False,
            target=target,
            target_matches=False,
            reason="junction points to %r, expected %r" % (target, final_path),
        )

    if probe_rel is None:
        probe_rel = find_probe(final_path)
    try:
        if probe_rel:
            linked = ops.read_bytes(_join_rel(link_path, probe_rel), 1)
            direct = ops.read_bytes(_join_rel(final_path, probe_rel), 1)
            if linked != direct:
                return LinkCheck(
                    ok=False,
                    target=target,
                    target_matches=True,
                    probe_rel=probe_rel,
                    probe_kind="file",
                    reason="probe read through the link differs from the target",
                )
            kind = "file"
        else:
            ops.list_dir(link_path)
            ops.list_dir(final_path)
            kind = "listing"
    except OSError as exc:
        return LinkCheck(
            ok=False,
            target=target,
            target_matches=True,
            probe_rel=probe_rel,
            probe_kind="file" if probe_rel else "listing",
            reason="probe failed: %s" % exc,
        )

    return LinkCheck(
        ok=True,
        target=target,
        target_matches=True,
        probe_rel=probe_rel,
        probe_kind=kind,
        reason="junction verified",
    )


def remove_own_link(ops, link_path, final_path):
    # type: (object, str, str) -> bool
    """Remove the link only when it is a junction pointing at ``final_path``.

    Returns False when no link exists (idempotent delete).  Raises
    ``LinkStepError`` for any other state, leaving the path untouched.
    """
    if not ops.exists(link_path):
        return False
    if not ops.is_junction(link_path):
        raise LinkStepError(
            "refusing to remove a non-junction path: %s" % link_path,
            mismatch=True,
        )
    target = ops.read_link_target(link_path)
    if not link_target_matches(target, final_path):
        raise LinkStepError(
            "refusing to remove junction %s pointing to %r"
            % (link_path, target),
            target=target,
            mismatch=True,
        )
    try:
        ops.remove_link(link_path, final_path)
    except OSError as exc:
        raise LinkStepError("cannot remove junction: %s" % exc)
    return True


__all__ = [
    "LinkCheck",
    "LinkStepError",
    "ensure_link",
    "find_probe",
    "remove_own_link",
    "verify_link",
]
