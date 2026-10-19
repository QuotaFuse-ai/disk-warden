# -*- coding: utf-8 -*-
"""Platform-independent process enumeration contract.

The platform adapters implement ``enumerate_processes`` and return this
model.  Business layers must not call ``psutil``/``tasklist``/``ps`` directly.

Python 3.8 compatible (decision 0005).
"""

from dataclasses import dataclass
from typing import Dict, Optional


class ProcessEnumerationError(RuntimeError):
    """Raised when the platform cannot produce a trustworthy process list."""


@dataclass(frozen=True)
class ProcessInfo:
    """One running process as reported by the operating system."""

    pid: int
    name: str
    exe: Optional[str] = None

    def to_dict(self):
        # type: () -> Dict[str, object]
        data = {"pid": self.pid, "name": self.name}
        if self.exe is not None:
            data["exe"] = self.exe
        return data


__all__ = ["ProcessEnumerationError", "ProcessInfo"]
