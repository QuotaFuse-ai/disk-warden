# -*- coding: utf-8 -*-
"""Portable filesystem primitives shared by the scan and migrate layers.

Only the operations needed today are exposed.  The important one is reparse
detection: Windows reports junctions through the reparse attribute and
``DirEntry.is_symlink()`` is ``False`` for them, so a traversal that only
checks ``is_symlink`` would walk into a junction, count the same bytes twice
and, for a self-referencing link, never terminate.

Python 3.8 compatible (decision 0005).
"""

import os
import stat

FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def is_reparse_stat(info):
    # type: (os.stat_result) -> bool
    """Return True when a ``stat_result`` describes a link/reparse point."""
    attributes = getattr(info, "st_file_attributes", 0) or 0
    return bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT) or stat.S_ISLNK(
        info.st_mode
    )


def is_reparse_point(path):
    # type: (str) -> bool
    """Return True when ``path`` itself is a junction/symlink/reparse point."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return is_reparse_stat(info)
