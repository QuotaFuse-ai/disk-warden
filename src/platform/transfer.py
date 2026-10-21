# -*- coding: utf-8 -*-
"""Platform-independent bulk transfer contract.

``run_robocopy`` is the only bulk-copy primitive the application may use; the
business layer never spawns ``robocopy`` itself (architecture 4.4).  The
Windows adapter returns the raw robocopy exit code so the caller can apply the
documented semantics (0-7 success, >= 8 failure, architecture 6.6).

Python 3.8 compatible (decision 0005).
"""

import sys


class TransferError(RuntimeError):
    """Base class for transfer adapter failures."""


class TransferUnsupportedError(TransferError):
    """Raised when the host has no bulk-transfer backend."""


class TransferCancelled(TransferError):
    """Raised when the transfer was stopped because the token fired."""


if sys.platform.startswith("win"):
    from .win.transfer import run_robocopy
elif sys.platform == "darwin":
    from .mac.transfer import run_robocopy
else:

    def run_robocopy(src, dst, log_path, cancel_token=None):
        # type: (str, str, str, object) -> int
        """Bulk transfer is a Windows-only primitive for now."""
        raise TransferUnsupportedError(
            "bulk transfer is not implemented on %s" % sys.platform
        )


__all__ = [
    "TransferCancelled",
    "TransferError",
    "TransferUnsupportedError",
    "run_robocopy",
]
