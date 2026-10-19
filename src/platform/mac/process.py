# -*- coding: utf-8 -*-
"""macOS process enumeration.

psutil is preferred; ``ps -axo pid=,comm=`` is the no-dependency fallback.
The catalog stores Windows executable names (``Cursor.exe``); the scan
layer normalises the ``.exe`` suffix before matching, so this adapter
reports names exactly as macOS presents them (``Cursor``).

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
            ["ps", "-axo", "pid=,comm="],
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
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 1)
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
