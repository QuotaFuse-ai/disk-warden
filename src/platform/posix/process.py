# -*- coding: utf-8 -*-
"""Generic POSIX process enumeration (development and CI only).

The product ships for Windows and macOS; this backend keeps unit tests and
Linux development machines working.  It is never selected on win32/darwin.

Python 3.8 compatible (decision 0005).
"""

import os
import subprocess
from typing import List

from ..process import ProcessEnumerationError, ProcessInfo

try:
    from .._psutil import enumerate_processes as _psutil_enumerate
except ImportError:  # pragma: no cover - depends on the installed environment
    _psutil_enumerate = None


def _enumerate_with_ps():
    # type: () -> List[ProcessInfo]
    try:
        completed = subprocess.run(
            ["ps", "-eo", "pid=,comm="],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise ProcessEnumerationError("ps failed to start: %s" % exc)

    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise ProcessEnumerationError(
            "ps exited with %d%s"
            % (
                completed.returncode,
                ": %s" % detail if detail else "",
            )
        )

    processes = []  # type: List[ProcessInfo]
    for line in completed.stdout.decode("utf-8", "replace").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        command = parts[1]
        processes.append(
            ProcessInfo(pid=pid, name=os.path.basename(command), exe=command)
        )
    return processes


def enumerate_processes():
    # type: () -> List[ProcessInfo]
    if _psutil_enumerate is not None:
        return _psutil_enumerate()
    return _enumerate_with_ps()


__all__ = ["enumerate_processes"]
