# -*- coding: utf-8 -*-
"""System adapter layer.

Only operating-system primitives live here (process enumeration, reparse
detection and bulk transfer today; junction/volume in later tasks).  Business
layers depend on this package; this package never imports business layers.

Python 3.8 compatible (decision 0005).
"""

import sys

from .filesystem import is_reparse_point, is_reparse_stat
from .link import (
    LinkError,
    VolumeInfo,
    link_target_matches,
    same_volume,
    strip_extended_prefix,
)
from .process import ProcessEnumerationError, ProcessInfo
from .reveal import RevealError, expand_path, reveal_directory
from .transfer import (
    TransferCancelled,
    TransferError,
    TransferUnsupportedError,
    run_robocopy,
)

if sys.platform.startswith("win"):
    from .win.process import enumerate_processes
elif sys.platform == "darwin":
    from .mac.process import enumerate_processes
else:
    from .posix.process import enumerate_processes

if sys.platform.startswith("win"):
    from .win.link import (
        create_junction,
        is_junction,
        is_link,
        read_junction_target,
        remove_junction,
        rename_dir,
        volume_info,
    )
    from .win.handles import find_dir_holders
else:
    from .posix.link import (
        create_junction,
        is_junction,
        is_link,
        read_junction_target,
        remove_junction,
        rename_dir,
        volume_info,
    )

    def find_dir_holders(path):
        # type: (str) -> list
        """POSIX renames do not fail because of open handles; nothing to find."""
        return []

__all__ = [
    "LinkError",
    "ProcessEnumerationError",
    "ProcessInfo",
    "RevealError",
    "TransferCancelled",
    "TransferError",
    "TransferUnsupportedError",
    "VolumeInfo",
    "create_junction",
    "enumerate_processes",
    "expand_path",
    "find_dir_holders",
    "is_junction",
    "is_link",
    "is_reparse_point",
    "is_reparse_stat",
    "link_target_matches",
    "read_junction_target",
    "remove_junction",
    "rename_dir",
    "reveal_directory",
    "run_robocopy",
    "same_volume",
    "strip_extended_prefix",
    "volume_info",
]
