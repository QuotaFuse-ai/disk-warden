# -*- coding: utf-8 -*-
"""Size and file-count measurement for catalog entries (T08).

The scan layer only produces facts: how many bytes and files live below a
path.  Safety classification stays in T07 and the decision to migrate stays in
T09.  The traversal is strictly read-only.

Guarantees:

* iterative (``os.scandir`` + explicit stack), so deep trees cannot exhaust the
  interpreter stack;
* reparse points (Windows junctions, symlinks) are never entered and never
  counted, so a link cannot double-count data or loop forever;
* one unreadable directory contributes ``access_errors`` and a warning, the
  rest of the scan continues;
* a cancelled token raises ``ScanCancelled`` at the next directory boundary —
  partial results are discarded by the caller;
* progress callbacks are throttled to at most ``1 / throttle_seconds`` per
  second (4/s by default) and always end with a final ``done`` event.

Byte totals are logical file sizes, the same quantity Explorer shows in its
"Size" column; directory metadata itself is not counted.

Python 3.8 compatible (decision 0005).
"""

import hashlib
import ntpath
import os
import posixpath
import re
import stat as stat_module
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from ...platform import is_reparse_stat
from .cancel import check_cancelled
from .models import STATUS_PRESENT, Warning

KIND_DIR = "dir"
KIND_FILE = "file"

WARNING_MISSING = "missing"
WARNING_PERMISSION_DENIED = "permission_denied"
WARNING_MEASURE_FAILED = "measure_failed"
WARNING_REPARSE_SKIPPED = "reparse_skipped"

DEFAULT_THROTTLE_SECONDS = 0.25

PLACEHOLDER_SEGMENT_RE = re.compile(r"^(?:<[^<>\s]+>)+$")
ENV_SEGMENT_RE = re.compile(r"^%[A-Za-z_][A-Za-z0-9_]*%$")


@dataclass(frozen=True)
class MeasureTarget:
    """One path to measure, usually built from a present catalog hit."""

    tool_id: str
    source_path: str
    rel_path: Optional[str] = None


@dataclass
class ScanItem:
    """Measured facts for one item, matching architecture §4.1."""

    item_id: str
    tool_id: str
    source_path: str
    rel_path: str
    kind: str
    bytes: int
    file_count: int
    is_reparse: bool
    access_errors: int
    mtime_max: Optional[str]

    def to_dict(self):
        # type: () -> Dict[str, object]
        return {
            "item_id": self.item_id,
            "tool_id": self.tool_id,
            "source_path": self.source_path,
            "rel_path": self.rel_path,
            "kind": self.kind,
            "bytes": self.bytes,
            "file_count": self.file_count,
            "is_reparse": self.is_reparse,
            "access_errors": self.access_errors,
            "mtime_max": self.mtime_max,
        }


@dataclass
class MeasureReport:
    """Result of ``measure``; also iterable so it can stand in for a list."""

    items: List[ScanItem] = field(default_factory=list)
    warnings: List[Warning] = field(default_factory=list)

    def __iter__(self):
        return iter(self.items)

    def __len__(self):
        return len(self.items)

    def __getitem__(self, index):
        return self.items[index]

    def summary(self):
        # type: () -> Dict[str, int]
        return {
            "items": len(self.items),
            "bytes": sum(item.bytes for item in self.items),
            "files": sum(item.file_count for item in self.items),
            "access_errors": sum(item.access_errors for item in self.items),
            "reparse_items": sum(1 for item in self.items if item.is_reparse),
            "warnings": len(self.warnings),
        }

    def to_dict(self):
        # type: () -> Dict[str, object]
        return {
            "summary": self.summary(),
            "items": [item.to_dict() for item in self.items],
            "warnings": [warning.to_dict() for warning in self.warnings],
        }


def make_item_id(tool_id, rel_path):
    # type: (str, str) -> str
    """Stable 16-hex-character id per architecture §4.1.

    Windows paths are case-insensitive, so the relative path is folded there;
    both separators are normalised to ``/``.
    """
    normalised = rel_path.replace("\\", "/")
    if os.name == "nt":
        normalised = normalised.lower()
    digest = hashlib.sha256(
        (tool_id + "\n" + normalised).encode("utf-8")
    ).hexdigest()
    return digest[:16]


def _isoformat(timestamp):
    # type: (Optional[float]) -> Optional[str]
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _is_dynamic_segment(segment):
    # type: (str) -> bool
    return (
        "*" in segment
        or "?" in segment
        or bool(PLACEHOLDER_SEGMENT_RE.match(segment))
        or bool(ENV_SEGMENT_RE.match(segment))
    )


def _path_module(pattern):
    # type: (str) -> object
    return ntpath if "\\" in pattern else posixpath


def _segments_after_tool_root(template):
    # type: (str) -> Optional[int]
    """Number of catalog segments below the tool root.

    ``%APPDATA%\\Cursor\\User\\workspaceStorage\\*.pack`` has three segments
    below ``Cursor``.  Every placeholder/wildcard matches exactly one concrete
    segment, so the same count can be sliced off the resolved path.  This is
    what keeps ``rel_path`` anchored to the tool directory instead of the
    expanded ``%APPDATA%`` prefix.
    """
    pathmod = _path_module(template)
    separator = "\\" if pathmod is ntpath else "/"
    _drive, tail = pathmod.splitdrive(template)
    segments = [segment for segment in tail.split(separator) if segment]
    for index, segment in enumerate(segments):
        if _is_dynamic_segment(segment):
            continue
        return len(segments) - index - 1
    return None


def _relative_path(template, resolved):
    # type: (str, str) -> str
    pathmod = _path_module(template)
    separator = "\\" if pathmod is ntpath else "/"
    depth = _segments_after_tool_root(template)
    if depth is None:
        return pathmod.basename(resolved)
    keep = max(depth, 1)
    parts = [part for part in resolved.replace("/", separator).split(separator) if part]
    if len(parts) < keep:
        return pathmod.basename(resolved)
    return "/".join(parts[-keep:])


def targets_from_discovery(discovery):
    # type: (object) -> List[MeasureTarget]
    """Flatten present catalog hits into measurement targets.

    ``rel_path`` is relative to the tool root (first static catalog segment) so
    the same file keeps the same ``item_id`` across scans and across the
    catalog's more specific entries.
    """
    targets = []  # type: List[MeasureTarget]
    for tool in discovery.tools:
        for hit in tool.directories:
            if hit.status != STATUS_PRESENT:
                continue
            for resolved in hit.resolved:
                targets.append(
                    MeasureTarget(
                        tool_id=tool.id,
                        source_path=resolved,
                        rel_path=_relative_path(hit.template, resolved),
                    )
                )
    return targets


class _ProgressEmitter(object):
    """Throttled progress callback wrapper; the final event is never dropped."""

    def __init__(self, callback, throttle_seconds):
        # type: (Optional[Callable[[Dict[str, object]], None]], float) -> None
        self._callback = callback
        self._interval = max(0.0, throttle_seconds)
        self._last_emit = 0.0
        self._started = time.monotonic()
        self._emitted_any = False

    def emit(self, payload, force=False):
        # type: (Dict[str, object], bool) -> None
        if self._callback is None:
            return
        now = time.monotonic()
        if (
            not force
            and self._emitted_any
            and now - self._last_emit < self._interval
        ):
            return
        self._last_emit = now
        self._emitted_any = True
        payload["elapsed_seconds"] = round(now - self._started, 3)
        self._callback(dict(payload))


def _warn(code, message, target, path=None):
    # type: (str, str, MeasureTarget, Optional[str]) -> Warning
    return Warning(
        code=code,
        message=message,
        tool_id=target.tool_id or None,
        path=path or target.source_path,
    )


def _coerce_target(value):
    # type: (object) -> MeasureTarget
    if isinstance(value, MeasureTarget):
        return value
    if isinstance(value, str):
        return MeasureTarget(
            tool_id="", source_path=value, rel_path=os.path.basename(value)
        )
    tool_id = getattr(value, "tool_id", "") or ""
    source_path = getattr(value, "source_path", None)
    if source_path is None:
        raise TypeError("target must be a path, MeasureTarget or scan item")
    return MeasureTarget(
        tool_id=tool_id,
        source_path=str(source_path),
        rel_path=getattr(value, "rel_path", None),
    )


def _empty_item(target, kind, info, access_errors, warnings):
    # type: (MeasureTarget, str, Optional[os.stat_result], int, List[Warning]) -> ScanItem
    rel_path = target.rel_path or os.path.basename(target.source_path)
    return ScanItem(
        item_id=make_item_id(target.tool_id, rel_path),
        tool_id=target.tool_id,
        source_path=target.source_path,
        rel_path=rel_path.replace("\\", "/"),
        kind=kind,
        bytes=0,
        file_count=0,
        is_reparse=bool(info is not None and is_reparse_stat(info)),
        access_errors=access_errors,
        mtime_max=_isoformat(info.st_mtime if info is not None else None),
    )


def _measure_target(target, cancel_token, emitter, counters):
    # type: (MeasureTarget, object, _ProgressEmitter, Dict[str, int]) -> Tuple[ScanItem, List[Warning]]
    warnings = []  # type: List[Warning]
    source_path = os.path.normpath(target.source_path)

    def progress(current_path):
        # type: (str) -> None
        emit_progress(emitter, target, counters, current_path, done=False)

    try:
        root_info = os.lstat(source_path)
    except FileNotFoundError:
        warnings.append(
            _warn(
                WARNING_MISSING,
                "path disappeared before it could be measured",
                target,
            )
        )
        return _empty_item(target, KIND_DIR, None, 1, warnings), warnings
    except PermissionError:
        warnings.append(
            _warn(
                WARNING_PERMISSION_DENIED,
                "access denied while measuring",
                target,
            )
        )
        return _empty_item(target, KIND_DIR, None, 1, warnings), warnings
    except OSError as exc:
        warnings.append(
            _warn(WARNING_MEASURE_FAILED, "cannot stat path: %s" % exc, target)
        )
        return _empty_item(target, KIND_DIR, None, 1, warnings), warnings

    is_dir = stat_module.S_ISDIR(root_info.st_mode)
    kind = KIND_DIR if is_dir else KIND_FILE

    if is_reparse_stat(root_info):
        warnings.append(
            _warn(
                WARNING_REPARSE_SKIPPED,
                "path is a junction/symlink and was not followed",
                target,
            )
        )
        return _empty_item(target, kind, root_info, 0, warnings), warnings

    total_bytes = 0
    file_count = 0
    access_errors = 0
    mtime_max = root_info.st_mtime

    if not is_dir:
        total_bytes = root_info.st_size
        file_count = 1
        counters["bytes_done"] += total_bytes
        counters["files_done"] += file_count
        progress(source_path)
        return (
            ScanItem(
                item_id=make_item_id(
                    target.tool_id, target.rel_path or os.path.basename(source_path)
                ),
                tool_id=target.tool_id,
                source_path=target.source_path,
                rel_path=(target.rel_path or os.path.basename(source_path)).replace(
                    "\\", "/"
                ),
                kind=kind,
                bytes=total_bytes,
                file_count=file_count,
                is_reparse=False,
                access_errors=0,
                mtime_max=_isoformat(mtime_max),
            ),
            warnings,
        )

    stack = [source_path]
    while stack:
        check_cancelled(cancel_token)
        directory = stack.pop()

        try:
            iterator = os.scandir(directory)
        except FileNotFoundError:
            access_errors += 1
            warnings.append(
                _warn(
                    WARNING_MISSING,
                    "directory disappeared during measurement",
                    target,
                    directory,
                )
            )
            continue
        except PermissionError:
            access_errors += 1
            warnings.append(
                _warn(
                    WARNING_PERMISSION_DENIED,
                    "access denied while listing directory",
                    target,
                    directory,
                )
            )
            continue
        except OSError as exc:
            access_errors += 1
            warnings.append(
                _warn(
                    WARNING_MEASURE_FAILED,
                    "cannot list directory: %s" % exc,
                    target,
                    directory,
                )
            )
            continue

        with iterator as entries:
            for entry in entries:
                check_cancelled(cancel_token)
                try:
                    entry_info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    access_errors += 1
                    warnings.append(
                        _warn(
                            WARNING_MISSING,
                            "entry disappeared during measurement",
                            target,
                            entry.path,
                        )
                    )
                    continue
                except PermissionError:
                    access_errors += 1
                    warnings.append(
                        _warn(
                            WARNING_PERMISSION_DENIED,
                            "access denied while reading entry",
                            target,
                            entry.path,
                        )
                    )
                    continue
                except OSError as exc:
                    access_errors += 1
                    warnings.append(
                        _warn(
                            WARNING_MEASURE_FAILED,
                            "cannot stat entry: %s" % exc,
                            target,
                            entry.path,
                        )
                    )
                    continue

                if is_reparse_stat(entry_info):
                    warnings.append(
                        _warn(
                            WARNING_REPARSE_SKIPPED,
                            "junction/symlink not followed",
                            target,
                            entry.path,
                        )
                    )
                    continue

                mtime_max = max(mtime_max, entry_info.st_mtime)

                if stat_module.S_ISDIR(entry_info.st_mode):
                    stack.append(entry.path)
                elif stat_module.S_ISREG(entry_info.st_mode):
                    total_bytes += entry_info.st_size
                    file_count += 1
                    counters["bytes_done"] += entry_info.st_size
                    counters["files_done"] += 1

                progress(entry.path)

    rel_path = target.rel_path or os.path.basename(source_path)
    return (
        ScanItem(
            item_id=make_item_id(target.tool_id, rel_path),
            tool_id=target.tool_id,
            source_path=target.source_path,
            rel_path=rel_path.replace("\\", "/"),
            kind=kind,
            bytes=total_bytes,
            file_count=file_count,
            is_reparse=False,
            access_errors=access_errors,
            mtime_max=_isoformat(mtime_max),
        ),
        warnings,
    )


def emit_progress(emitter, target, counters, current_path, done):
    # type: (_ProgressEmitter, MeasureTarget, Dict[str, int], str, bool) -> None
    emitter.emit(
        {
            "targets_total": counters["targets_total"],
            "targets_done": counters["targets_done"],
            "tool_id": target.tool_id,
            "path": current_path,
            "bytes_done": counters["bytes_done"],
            "files_done": counters["files_done"],
            "done": done,
        },
        force=done,
    )


def measure(targets, cancel_token=None, progress_cb=None,
            throttle_seconds=DEFAULT_THROTTLE_SECONDS):
    # type: (Iterable[object], object, Optional[Callable[[Dict[str, object]], None]], float) -> MeasureReport
    """Measure every target, returning a ``MeasureReport``.

    ``targets`` accepts ``MeasureTarget`` objects, plain paths or anything with
    ``tool_id``/``source_path``/``rel_path`` attributes (e.g. a future scan
    snapshot).  ``cancel_token`` may be a ``CancelToken`` or a
    ``threading.Event``.  Raises ``ScanCancelled`` as soon as cancellation is
    observed.
    """
    target_list = [_coerce_target(target) for target in targets]
    report = MeasureReport()
    emitter = _ProgressEmitter(progress_cb, throttle_seconds)
    counters = {
        "targets_total": len(target_list),
        "targets_done": 0,
        "bytes_done": 0,
        "files_done": 0,
    }

    for target in target_list:
        check_cancelled(cancel_token)
        emit_progress(emitter, target, counters, target.source_path, done=False)
        item, item_warnings = _measure_target(
            target, cancel_token, emitter, counters
        )
        check_cancelled(cancel_token)
        report.items.append(item)
        report.warnings.extend(item_warnings)
        counters["targets_done"] += 1
        emit_progress(
            emitter,
            target,
            counters,
            target.source_path,
            done=counters["targets_done"] == counters["targets_total"],
        )

    if not target_list:
        emitter.emit(
            {
                "targets_total": 0,
                "targets_done": 0,
                "tool_id": "",
                "path": "",
                "bytes_done": 0,
                "files_done": 0,
                "done": True,
            },
            force=True,
        )

    return report
