# -*- coding: utf-8 -*-
"""macOS bulk transfer via ``rsync``.

``rsync -a`` copies the *contents* of the source into the destination, which
is the same contract as ``robocopy src dst /E``: the caller hands the source
directory and the (empty) staging directory, and verification compares both
trees.  The source is passed with a trailing slash for that reason.

Exit-code contract: the caller applies the documented "0-7 success, >= 8
failure" rule (architecture 6.6), so this adapter normalises rsync's own
numbering to it:

* 0 (success) and 24 ("some files vanished before they could be transferred")
  become 0 -- a vanished cache file is not a copy failure; verification is
  authoritative and rejects a staging tree that really is incomplete;
* every other code is an error and is reported as ``max(code, 8)`` so the
  business layer's rule stays meaningful and ``code`` keeps the detail.

rsync never deletes destination files (no ``--delete``) and ``-a`` copies
symlinks as symlinks instead of following them.

Python 3.8 compatible (decision 0005).
"""

import subprocess

from ..transfer import TransferCancelled, TransferError

RSYNC_ARGUMENTS = ("-a", "--numeric-ids")
RSYNC_SUCCESS_CODES = (0, 24)
FAILURE_FLOOR = 8
_POLL_SECONDS = 0.2


def _token_fired(token):
    # type: (object) -> bool
    if token is None:
        return False
    checker = getattr(token, "is_set", None) or getattr(
        token, "is_cancelled", None
    )
    return bool(checker()) if checker is not None else False


def _stop(process):
    # type: (subprocess.Popen) -> None
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover - kill is immediate
        pass


def normalise_exit_code(code):
    # type: (int) -> int
    """Map rsync's exit code onto the portable 0-7 / >= 8 contract."""
    if code in RSYNC_SUCCESS_CODES:
        return 0
    return code if code >= FAILURE_FLOOR else FAILURE_FLOOR


def _source_argument(source):
    # type: (str) -> str
    """``rsync`` copies a directory's *contents* only with a trailing slash.

    The separator is the POSIX one on purpose: this adapter only ever runs on
    macOS, where ``\\`` is an ordinary filename character.
    """
    stripped = source.rstrip("/")
    if not stripped:
        return "/"
    return stripped + "/"


def run_robocopy(src, dst, log_path, cancel_token=None):
    # type: (str, str, str, object) -> int
    """Copy ``src`` into ``dst`` with ``rsync`` and return a normalised code.

    Raises ``TransferCancelled`` when the token fires (the process is killed)
    and ``TransferError`` when rsync cannot be started.  The raw rsync code is
    written to ``log_path`` by rsync itself.
    """
    if not src or not dst or not log_path:
        raise ValueError("src, dst and log_path must be non-empty")

    arguments = ["rsync"] + list(RSYNC_ARGUMENTS)
    arguments.append("--log-file=%s" % log_path)
    arguments.append("--")
    arguments.append(_source_argument(src))
    arguments.append(dst)

    try:
        process = subprocess.Popen(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as exc:
        raise TransferError("rsync failed to start: %s" % exc)

    while True:
        try:
            return normalise_exit_code(process.wait(timeout=_POLL_SECONDS))
        except subprocess.TimeoutExpired:
            if _token_fired(cancel_token):
                _stop(process)
                raise TransferCancelled("rsync cancelled")
        except BaseException:  # pragma: no cover - defensive cleanup
            _stop(process)
            raise


__all__ = [
    "RSYNC_ARGUMENTS",
    "RSYNC_SUCCESS_CODES",
    "normalise_exit_code",
    "run_robocopy",
]
