# -*- coding: utf-8 -*-
"""psutil-backed process enumeration shared by the Windows/macOS adapters.

Importing this module raises ``ImportError`` when psutil is not installed;
each adapter treats that as a signal to fall back to the system command
(``tasklist`` / ``ps``).

Python 3.8 compatible (decision 0005).
"""

from typing import List

import psutil

from .process import ProcessInfo


def enumerate_processes():
    # type: () -> List[ProcessInfo]
    processes = []  # type: List[ProcessInfo]

    # psutil.process_iter already skips processes that vanish mid-iteration.
    for process in psutil.process_iter(["pid", "name", "exe"]):
        try:
            info = process.info
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

        pid = info.get("pid")
        name = info.get("name") or ""
        exe = info.get("exe")

        if not name and exe:
            # Some platforms omit the display name; derive it from the path.
            name = str(exe).replace("\\", "/").rsplit("/", 1)[-1]
        if pid is None or not name:
            continue

        processes.append(ProcessInfo(pid=int(pid), name=str(name), exe=exe))

    return processes


__all__ = ["enumerate_processes"]
