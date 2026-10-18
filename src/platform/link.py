# -*- coding: utf-8 -*-
"""Portable link and volume primitives shared by the migration engine.

The business layer never calls ``subprocess``/``ctypes`` itself: junction
creation, target read-back, link removal, atomic rename and volume identity
all live behind this module (decision 0004).

``read_junction_target`` returns the platform's raw target with the Windows
``\\\\?\\`` prefix stripped.  Junction targets are frequently reported as 8.3
short paths, so callers compare targets with :func:`link_target_matches`,
which canonicalises both sides instead of comparing strings.

Python 3.8 compatible (decision 0005).
"""

import os
import sys
from dataclasses import dataclass

EXTENDED_PREFIX = "\\\\?\\"
EXTENDED_UNC_PREFIX = "\\\\?\\UNC\\"


class LinkError(OSError):
    """Raised when a link or volume primitive cannot do what was asked."""


@dataclass(frozen=True)
class VolumeInfo:
    """Identity of the volume that contains a path."""

    mount_point: str
    fs_name: str
    serial: int

    @property
    def is_ntfs(self):
        # type: () -> bool
        return self.fs_name.upper() == "NTFS"

    def to_dict(self):
        # type: () -> dict
        return {
            "mount_point": self.mount_point,
            "fs_name": self.fs_name,
            "serial": self.serial,
            "is_ntfs": self.is_ntfs,
        }


def strip_extended_prefix(path):
    # type: (str) -> str
    """Remove the ``\\\\?\\`` / ``\\\\?\\UNC\\`` prefix Windows adds."""
    if not path:
        return path
    if path.startswith(EXTENDED_UNC_PREFIX):
        return "\\\\" + path[len(EXTENDED_UNC_PREFIX):]
    if path.startswith(EXTENDED_PREFIX):
        return path[len(EXTENDED_PREFIX):]
    return path


def canonical_path(path):
    # type: (str) -> str
    """Resolve a path for comparison (short names, case, relative parts)."""
    value = strip_extended_prefix(str(path))
    try:
        value = os.path.realpath(value)
    except OSError:
        value = os.path.abspath(value)
    return os.path.normcase(os.path.normpath(value))


def link_target_matches(actual, expected):
    # type: (str, str) -> bool
    """True when a link's read-back target resolves to ``expected``."""
    if not actual or not expected:
        return False
    return canonical_path(actual) == canonical_path(expected)


def _volume_info():
    # type: () -> object
    # Deferred: the platform adapter imports this module, so resolving it at
    # call time avoids the package-initialisation cycle.
    if sys.platform.startswith("win"):
        from .win.link import volume_info
    else:
        from .posix.link import volume_info
    return volume_info


def same_volume(left, right):
    # type: (str, str) -> bool
    """Best-effort comparison of the volumes holding two paths."""
    volume_info = _volume_info()
    try:
        left_info = volume_info(left)
        right_info = volume_info(right)
    except (OSError, LinkError):
        return False
    return (
        left_info.serial == right_info.serial
        and canonical_path(left_info.mount_point)
        == canonical_path(right_info.mount_point)
    )


__all__ = [
    "EXTENDED_PREFIX",
    "EXTENDED_UNC_PREFIX",
    "LinkError",
    "VolumeInfo",
    "canonical_path",
    "link_target_matches",
    "same_volume",
    "strip_extended_prefix",
]
