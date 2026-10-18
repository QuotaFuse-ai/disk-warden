# -*- coding: utf-8 -*-
"""Process-occupancy detection for catalog tools (T08).

The list of executable names comes exclusively from
``data/ide-catalog.json`` (``tool.processes``); this module contains no product
names of its own.  Matching is case-insensitive and ignores the ``.exe``
suffix, so the same catalog works on Windows, macOS and Linux.

A process list that cannot be obtained is reported as ``unknown`` and counts
as blocking.  Product spec §5.1 forbids treating "detection failed" as "not
running": a migration must never touch files that may still be open.

Python 3.8 compatible (decision 0005).
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

STATUS_RUNNING = "running"
STATUS_NOT_RUNNING = "not_running"
STATUS_NO_PROCESS_NAMES = "no_process_names"
STATUS_UNKNOWN = "unknown"

STATUS_VALUES = (
    STATUS_RUNNING,
    STATUS_NOT_RUNNING,
    STATUS_NO_PROCESS_NAMES,
    STATUS_UNKNOWN,
)

STATUS_LABELS = {
    STATUS_RUNNING: "运行中",
    STATUS_NOT_RUNNING: "未运行",
    STATUS_NO_PROCESS_NAMES: "目录清单未定义进程名",
    STATUS_UNKNOWN: "状态未知",
}


def _process_stem(value):
    # type: (str) -> str
    """Basename without directory parts and without a trailing ``.exe``."""
    name = str(value or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name.lower()


@dataclass(frozen=True)
class ProcessHit:
    """Occupancy verdict for one tool."""

    tool_id: str
    status: str
    catalog_names: Tuple[str, ...] = ()
    matched: Tuple[Any, ...] = ()
    error: Optional[str] = field(default=None)

    @property
    def blocked_by_process(self):
        # type: () -> bool
        """True when migration must be blocked (running or undetectable)."""
        return self.status in (STATUS_RUNNING, STATUS_UNKNOWN)

    @property
    def pids(self):
        # type: () -> List[int]
        return [int(getattr(process, "pid", 0)) for process in self.matched]

    def to_dict(self):
        # type: () -> Dict[str, object]
        data = {
            "tool_id": self.tool_id,
            "status": self.status,
            "status_label": STATUS_LABELS[self.status],
            "blocked_by_process": self.blocked_by_process,
            "catalog_names": list(self.catalog_names),
            "pids": self.pids,
            "processes": [
                process.to_dict()
                if hasattr(process, "to_dict")
                else {"pid": getattr(process, "pid", None),
                      "name": getattr(process, "name", "")}
                for process in self.matched
            ],
        }
        if self.error is not None:
            data["error"] = self.error
        return data


def _default_provider():
    # type: () -> Iterable[Any]
    from ...platform import enumerate_processes

    return enumerate_processes()


def _tool_list(tools):
    # type: (object) -> List[Any]
    if hasattr(tools, "tools"):
        return list(tools.tools)
    return list(tools)


def detect_processes(tools, process_provider=None):
    # type: (object, Optional[Callable[[], Iterable[Any]]]) -> Dict[str, ProcessHit]
    """Return one ``ProcessHit`` per catalog tool, keyed by ``tool_id``.

    ``tools`` may be a ``Catalog`` or any iterable of objects with ``id`` and
    ``processes`` (``CatalogTool``).  ``process_provider`` is injectable for
    tests and must return objects with ``pid``/``name``/``exe``.
    """
    tool_list = _tool_list(tools)
    provider = process_provider or _default_provider

    try:
        processes = list(provider())
        enumeration_error = None
    except Exception as exc:  # provider failure must not abort the scan
        processes = []
        enumeration_error = str(exc) or exc.__class__.__name__

    hits = {}  # type: Dict[str, ProcessHit]
    for tool in tool_list:
        names = tuple(getattr(tool, "processes", ()) or ())
        tool_id = str(getattr(tool, "id", ""))

        if enumeration_error is not None:
            hits[tool_id] = ProcessHit(
                tool_id=tool_id,
                status=STATUS_UNKNOWN,
                catalog_names=names,
                error=enumeration_error,
            )
            continue

        if not names:
            hits[tool_id] = ProcessHit(
                tool_id=tool_id,
                status=STATUS_NO_PROCESS_NAMES,
                catalog_names=(),
            )
            continue

        wanted = {_process_stem(name) for name in names if _process_stem(name)}
        matched = tuple(
            process
            for process in processes
            if _matches_process(process, wanted)
        )
        hits[tool_id] = ProcessHit(
            tool_id=tool_id,
            status=STATUS_RUNNING if matched else STATUS_NOT_RUNNING,
            catalog_names=names,
            matched=matched,
        )

    return hits


def _matches_process(process, wanted):
    # type: (Any, set) -> bool
    name = _process_stem(getattr(process, "name", "") or "")
    if name and name in wanted:
        return True
    exe = _process_stem(getattr(process, "exe", "") or "")
    return bool(exe and exe in wanted)


def blocked_tool_ids(hits):
    # type: (Dict[str, ProcessHit]) -> List[str]
    """Tool ids whose occupancy blocks a migration (running or unknown)."""
    return sorted(
        tool_id
        for tool_id, hit in hits.items()
        if hit.blocked_by_process
    )


def summarize(hits):
    # type: (Dict[str, ProcessHit]) -> Dict[str, int]
    summary = {"tools": len(hits), "blocked": 0}
    for status in STATUS_VALUES:
        summary[status] = sum(1 for hit in hits.values() if hit.status == status)
    summary["blocked"] = len(blocked_tool_ids(hits))
    return summary
