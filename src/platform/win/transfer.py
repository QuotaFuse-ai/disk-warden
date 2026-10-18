# -*- coding: utf-8 -*-
"""Windows bulk transfer via ``robocopy``.

The adapter returns the raw exit code and deliberately does not classify it:
the business layer owns the "0-7 success, >= 8 failure" rule so the contract
stays visible where the safety decision is made.

Arguments follow architecture 6.6.  ``/XJ`` keeps junctions out of the copy,
``/E`` copies empty directories, ``/R:2 /W:2`` bounds retries, and ``/LOG``
keeps the evidence.  ``/MIR`` is never used: it would delete files on the
destination side.

Python 3.8 compatible (decision 0005).
"""

import subprocess

from ..transfer import TransferCancelled, TransferError

ROBOCOPY_ARGUMENTS = (
    "/E",
    "/COPY:DAT",
    "/DCOPY:T",
    "/XJ",
    "/R:2",
    "/W:2",
    "/NP",
    "/NFL",
    "/NDL",
    "/NJH",
    "/NJS",
    "/BYTES",
)

# Avoid flashing a console window when the host runs without one.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
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


def run_robocopy(src, dst, log_path, cancel_token=None):
    # type: (str, str, str, object) -> int
    """Copy ``src`` to ``dst`` and return robocopy's exit code.

    Raises ``TransferCancelled`` when the token fires (the process is killed)
    and ``TransferError`` when robocopy cannot be started.
    """
    if not src or not dst or not log_path:
        raise ValueError("src, dst and log_path must be non-empty")

    arguments = ["robocopy", src, dst] + list(ROBOCOPY_ARGUMENTS)
    arguments.append("/LOG:%s" % log_path)

    try:
        process = subprocess.Popen(
            arguments,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
        )
    except OSError as exc:
        raise TransferError("robocopy failed to start: %s" % exc)

    while True:
        try:
            return process.wait(timeout=_POLL_SECONDS)
        except subprocess.TimeoutExpired:
            if _token_fired(cancel_token):
                _stop(process)
                raise TransferCancelled("robocopy cancelled")
        except BaseException:  # pragma: no cover - defensive cleanup
            _stop(process)
            raise


__all__ = ["ROBOCOPY_ARGUMENTS", "run_robocopy"]
