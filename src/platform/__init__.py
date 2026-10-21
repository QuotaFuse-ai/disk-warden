# -*- coding: utf-8 -*-
"""System adapter layer.

Only operating-system primitives live here (process enumeration, reparse
detection and bulk transfer today; junction/volume in later tasks).  Business
layers depend on this package; this package never imports business layers.

Python 3.8 compatible (decision 0005).
"""

import sys

from .external import ExternalOpenError, open_url
from .filesystem import is_reparse_point, is_reparse_stat
from .link import (
    LinkError,
    VolumeInfo,
    link_support,
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

# macOS version facts and the unsigned-app first-launch steps.  Only the
# darwin adapter knows about Gatekeeper; every other host gets a stub so the
# business layer can call one function without branching on the platform.
if sys.platform == "darwin":
    from .mac.version import first_launch_guide, macos_version
else:

    def macos_version():
        # type: () -> tuple
        """Not macOS; there is no version to report."""
        return (0, 0, 0)

    def first_launch_guide(first_run_done=False, version=None):
        # type: (bool, object) -> dict
        """Unsigned-app hurdles are macOS-specific.

        SmartScreen is handled by ``START-HERE.txt`` and the launcher, so
        Windows has no version-dependent first-launch steps to explain.
        """
        return {
            "show": False,
            "platform": sys.platform,
            "version": "",
            "major": 0,
            "tier": "none",
            "steps": [],
            "advanced_command": "",
            "min_supported": 0,
            "supported": True,
        }


if sys.platform.startswith("win"):
    from .win.autostart import Autostart, AutostartError
    from .win.process import enumerate_processes
elif sys.platform == "darwin":
    from .mac.autostart import Autostart, AutostartError
    from .mac.process import enumerate_processes
else:
    from .autostart import Autostart, AutostartError
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
    from .win.integrity import describe_denial as describe_integrity_denial
elif sys.platform == "darwin":
    from .mac.link import (
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
        """macOS renames do not fail because of open handles."""
        return []

    def describe_integrity_denial(path):
        # type: (str) -> str
        """macOS has no mandatory integrity levels; nothing can be denied."""
        return ""
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

    def describe_integrity_denial(path):
        # type: (str) -> str
        """POSIX has no mandatory integrity levels; nothing can be denied."""
        return ""

__all__ = [
    "Autostart",
    "AutostartError",
    "ExternalOpenError",
    "LinkError",
    "ProcessEnumerationError",
    "ProcessInfo",
    "RevealError",
    "TransferCancelled",
    "TransferError",
    "TransferUnsupportedError",
    "VolumeInfo",
    "create_junction",
    "describe_integrity_denial",
    "enumerate_processes",
    "expand_path",
    "find_dir_holders",
    "first_launch_guide",
    "is_junction",
    "is_link",
    "is_reparse_point",
    "is_reparse_stat",
    "link_support",
    "link_target_matches",
    "macos_version",
    "open_url",
    "read_junction_target",
    "remove_junction",
    "rename_dir",
    "reveal_directory",
    "run_robocopy",
    "same_volume",
    "strip_extended_prefix",
    "volume_info",
]
