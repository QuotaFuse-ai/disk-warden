# -*- coding: utf-8 -*-
"""Cancellation primitives for long-running scans.

The app layer owns the real token (a ``threading.Event`` works as-is); this
module only defines the exception the scan layer raises when a token fires and
a tiny standalone token for CLI/test use.

Python 3.8 compatible (decision 0005).
"""


class ScanCancelled(Exception):
    """Raised when a running scan observes a cancelled token.

    Partial results are discarded by the caller: ``measure`` never returns a
    half-measured report.
    """


class CancelToken(object):
    """Minimal thread-safe-free token with the same shape as ``Event``."""

    def __init__(self):
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def is_cancelled(self):
        # type: () -> bool
        return self._cancelled

    # ``threading.Event`` compatibility so callers can pass either type.
    def is_set(self):
        # type: () -> bool
        return self._cancelled


def is_cancelled(token):
    # type: (object) -> bool
    if token is None:
        return False
    checker = getattr(token, "is_set", None) or getattr(
        token, "is_cancelled", None
    )
    return bool(checker()) if checker is not None else False


def check_cancelled(token):
    # type: (object) -> None
    if is_cancelled(token):
        raise ScanCancelled("scan cancelled")
