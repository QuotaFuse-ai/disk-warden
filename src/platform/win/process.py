# -*- coding: utf-8 -*-
"""Windows process enumeration.

psutil is the preferred backend (architecture decision 0003).  When it is
not available the adapter falls back to ``tasklist``, which always exists on
Windows.  ``tasklist`` cannot report executable paths, so ``exe`` stays
``None`` in that mode.

Python 3.8 compatible (decision 0005).
"""

import csv
import io
import subprocess
from typing import List

from ..process import ProcessEnumerationError, ProcessInfo

try:
    from .._psutil import enumerate_processes as _psutil_enumerate
except ImportError:  # pragma: no cover - depends on the installed environment
    _psutil_enumerate = None

# Avoid flashing a console window when the host runs without one.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _decode_output(data):
    # type: (bytes) -> str
    try:
        return data.decode("mbcs", "replace")
    except LookupError:  # pragma: no cover - mbcs exists on Windows
        return data.decode("utf-8", "replace")


def _enumerate_with_tasklist():
    # type: () -> List[ProcessInfo]
    try:
        completed = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=_CREATE_NO_WINDOW,
        )
    except OSError as exc:
        raise ProcessEnumerationError("tasklist failed to start: %s" % exc)

    if completed.returncode != 0:
        detail = _decode_output(completed.stderr).strip()
        raise ProcessEnumerationError(
            "tasklist exited with %d%s"
            % (
                completed.returncode,
                ": %s" % detail if detail else "",
            )
        )

    processes = []  # type: List[ProcessInfo]
    reader = csv.reader(io.StringIO(_decode_output(completed.stdout)))
    for row in reader:
        if len(row) < 2:
            continue
        try:
            pid = int(row[1])
        except ValueError:
            continue
        processes.append(ProcessInfo(pid=pid, name=row[0]))
    return processes


def enumerate_processes():
    # type: () -> List[ProcessInfo]
    if _psutil_enumerate is not None:
        return _psutil_enumerate()
    return _enumerate_with_tasklist()


__all__ = ["enumerate_processes"]
