# -*- coding: utf-8 -*-
"""POSIX/macOS link and volume primitives.

macOS and Linux have no junction; a directory symlink provides the same
"source path points at the new location" behaviour.  The migration layer only
uses the portable contract, so the same engine runs on both platforms
(decision 0004; the full macOS adapter is T21).

Python 3.8 compatible (decision 0005).
"""

import os

from ..link import LinkError, VolumeInfo, link_target_matches


def is_junction(path):
    # type: (str) -> bool
    """True for a symlink that resolves to a directory."""
    try:
        return os.path.islink(path) and os.path.isdir(path)
    except OSError:
        return False


def is_link(path):
    # type: (str) -> bool
    try:
        return os.path.islink(path)
    except OSError:
        return False


def create_junction(link_path, target_path):
    # type: (str, str) -> str
    link_path = os.path.abspath(os.path.normpath(link_path))
    target_path = os.path.abspath(os.path.normpath(target_path))
    if not os.path.isdir(target_path):
        raise LinkError("link target is not a directory: %s" % target_path)
    if os.path.lexists(link_path):
        raise LinkError("link path already exists: %s" % link_path)
    if not os.path.isdir(os.path.dirname(link_path)):
        raise LinkError(
            "link parent does not exist: %s" % os.path.dirname(link_path)
        )
    try:
        os.symlink(target_path, link_path, target_is_directory=True)
    except OSError as exc:
        raise LinkError("symlink failed: %s" % exc)
    return link_path


def read_junction_target(path):
    # type: (str) -> str
    try:
        return os.readlink(path)
    except OSError:
        return None


def remove_junction(path, expected_target=None):
    # type: (str, str) -> bool
    if not os.path.islink(path):
        if not os.path.lexists(path):
            return False
        raise LinkError("refusing to remove a non-link path: %s" % path)
    if expected_target is not None:
        actual = read_junction_target(path)
        if not link_target_matches(actual, expected_target):
            raise LinkError(
                "link %s points to %r, expected %r"
                % (path, actual, expected_target)
            )
    os.unlink(path)
    return True


def rename_dir(source, destination):
    # type: (str, str) -> str
    if os.path.lexists(destination):
        raise LinkError("rename destination already exists: %s" % destination)
    os.rename(source, destination)
    return destination


def volume_info(path):
    # type: (str) -> VolumeInfo
    info = os.stat(path)
    return VolumeInfo(
        mount_point=os.path.abspath(os.sep),
        fs_name="",
        serial=int(info.st_dev),
    )


def link_support(path):
    # type: (str) -> dict
    """Generic POSIX: directory symlinks are expected to work.

    The development/CI fallback has no filesystem name to inspect; creation
    reports the real error when a filesystem refuses symlinks.
    """
    info = volume_info(path)
    return {
        "supported": True,
        "code": "",
        "message": "",
        "fs_name": info.fs_name,
        "mount_point": info.mount_point,
    }


__all__ = [
    "create_junction",
    "is_junction",
    "is_link",
    "link_support",
    "read_junction_target",
    "remove_junction",
    "rename_dir",
    "volume_info",
]
