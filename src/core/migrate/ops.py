# -*- coding: utf-8 -*-
"""Injectable filesystem primitives for the migration half (S5-S9).

Every rename, link and read the engine performs goes through this object, so
tests and fault drills can substitute one operation at a time (architecture
6.9).  The default delegates to ``src.platform``; business modules never call
``subprocess``/``ctypes`` themselves (decision 0004).

Python 3.8 compatible (decision 0005).
"""

import os

from ...platform import (
    create_junction,
    is_junction,
    is_link,
    read_junction_target,
    remove_junction,
    rename_dir,
    same_volume,
    volume_info,
)


class MigrationOps(object):
    """Filesystem operations used by swap/link/recovery."""

    def exists(self, path):
        # type: (str) -> bool
        return os.path.lexists(path)

    def is_dir(self, path):
        # type: (str) -> bool
        return os.path.isdir(path)

    def is_link(self, path):
        # type: (str) -> bool
        return is_link(path)

    def is_junction(self, path):
        # type: (str) -> bool
        return is_junction(path)

    def read_link_target(self, path):
        # type: (str) -> str
        return read_junction_target(path)

    def create_link(self, link_path, target_path):
        # type: (str, str) -> str
        return create_junction(link_path, target_path)

    def remove_link(self, path, expected_target=None):
        # type: (str, str) -> bool
        return remove_junction(path, expected_target)

    def rename(self, source, destination):
        # type: (str, str) -> str
        return rename_dir(source, destination)

    def same_volume(self, left, right):
        # type: (str, str) -> bool
        return same_volume(left, right)

    def volume_info(self, path):
        # type: (str) -> object
        return volume_info(path)

    def read_bytes(self, path, size=1):
        # type: (str, int) -> bytes
        with open(path, "rb") as handle:
            return handle.read(size)

    def list_dir(self, path):
        # type: (str) -> list
        with os.scandir(path) as entries:
            return sorted(entry.name for entry in entries)


__all__ = ["MigrationOps"]
