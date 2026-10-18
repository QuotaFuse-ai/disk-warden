# -*- coding: utf-8 -*-
"""Catalog-driven discovery: which known IDEs/CLIs exist on this machine.

T06 scope: existence checks only.  No size measurement, no process detection,
no rules classification.  The implementation is strictly read-only: it calls
``os.stat`` and ``os.scandir`` and never creates, opens for writing, renames or
deletes anything.

Python 3.8 compatible (decision 0005).
"""

import fnmatch
import ntpath
import os
import re
import stat as stat_module
from typing import Dict, List, Mapping, Optional, Tuple

from .env import UnknownEnvironmentVariable, expand_environment
from .models import (
    Catalog,
    CatalogDirectory,
    PathHit,
    ScanDiscovery,
    STATUS_ABSENT,
    STATUS_NO_PERMISSION,
    STATUS_PRESENT,
    ToolDiscovery,
    Warning,
)

PLACEHOLDER_SEGMENT_RE = re.compile(r"^(?:<[^<>\s]+>)+$")

# Error detail codes carried on PathHit.detail.
DETAIL_NOT_FOUND = "not_found"
DETAIL_TYPE_MISMATCH = "type_mismatch"
DETAIL_PERMISSION_DENIED = "permission_denied"
DETAIL_UNRESOLVED_ENVIRONMENT = "unresolved_environment"


def _is_placeholder_segment(segment):
    # type: (str) -> bool
    return bool(PLACEHOLDER_SEGMENT_RE.match(segment))


def _split_pattern(pattern):
    # type: (str) -> Tuple[str, List[str]]
    """Split an absolute Windows path into drive and non-empty segments."""
    drive, tail = ntpath.splitdrive(pattern)
    segments = [segment for segment in tail.split("\\") if segment]
    return drive, segments


def _root_prefix(drive):
    # type: (str) -> str
    return drive + "\\" if drive else ""


def _join(base, segment):
    # type: (str, str) -> str
    return ntpath.join(base, segment) if base else segment


def _list_children(base):
    # type: (str) -> Tuple[List[Tuple[str, str, bool]], bool, Optional[BaseException]]
    """List immediate children of ``base`` as ``(name, path, is_dir)`` tuples.

    Missing parents yield an empty list and are not treated as an error;
    permission problems set ``denied``.  DirEntry values are copied inside the
    iterator scope so nothing is used after the handle is closed.
    """
    entries = []  # type: List[Tuple[str, str, bool]]
    try:
        with os.scandir(base) as iterator:
            for child in iterator:
                try:
                    is_dir = child.is_dir()
                except OSError:
                    is_dir = False
                entries.append((child.name, child.path, is_dir))
        return entries, False, None
    except (FileNotFoundError, NotADirectoryError):
        return [], False, None
    except PermissionError as exc:
        return [], True, exc
    except OSError as exc:
        return [], True, exc


def _matches_expected_type(entry_type, is_dir):
    # type: (str, bool) -> bool
    if entry_type == "dir":
        return is_dir
    if entry_type == "file":
        return not is_dir
    return True


def _probe(entry, environ):
    # type: (CatalogDirectory, Optional[Mapping[str, str]]) -> Tuple[PathHit, List[Warning]]
    """Resolve one catalog entry and report its existence status."""
    warnings = []  # type: List[Warning]

    try:
        pattern = expand_environment(entry.path, environ)
    except UnknownEnvironmentVariable as exc:
        warnings.append(
            Warning(
                code=DETAIL_UNRESOLVED_ENVIRONMENT,
                message="environment variable %%%s%% is not set" % exc.name,
                path=entry.path,
            )
        )
        return (
            PathHit(
                template=entry.path,
                pattern=entry.path,
                type=entry.type,
                safety=entry.safety,
                reason=entry.reason,
                handling=entry.handling,
                status=STATUS_ABSENT,
                detail=DETAIL_UNRESOLVED_ENVIRONMENT,
            ),
            warnings,
        )

    drive, segments = _split_pattern(pattern)
    bases = [_root_prefix(drive)]
    denied = False
    error_detail = None  # type: Optional[str]
    type_mismatch = False

    for index, segment in enumerate(segments):
        last = index == len(segments) - 1

        if _is_placeholder_segment(segment):
            next_bases = []  # type: List[str]
            for base in bases:
                children, was_denied, error = _list_children(base)
                if was_denied:
                    denied = True
                    if error_detail is None:
                        error_detail = str(error)
                for name, child_path, child_is_dir in children:
                    if not _matches_expected_type(
                        entry.type if last else "dir", child_is_dir
                    ):
                        continue
                    next_bases.append(child_path)
            bases = next_bases

        elif "*" in segment or "?" in segment:
            next_bases = []
            for base in bases:
                children, was_denied, error = _list_children(base)
                if was_denied:
                    denied = True
                    if error_detail is None:
                        error_detail = str(error)
                for name, child_path, child_is_dir in children:
                    if not fnmatch.fnmatch(name, segment):
                        continue
                    if not last and not child_is_dir:
                        continue
                    next_bases.append(child_path)
            bases = next_bases

        else:
            bases = [_join(base, segment) for base in bases]

    resolved = []  # type: List[str]
    for base in bases:
        try:
            info = os.stat(base)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except PermissionError as exc:
            denied = True
            if error_detail is None:
                error_detail = str(exc)
            continue
        except OSError as exc:
            denied = True
            if error_detail is None:
                error_detail = str(exc)
            continue

        is_dir = stat_module.S_ISDIR(info.st_mode)
        if not _matches_expected_type(entry.type, is_dir):
            type_mismatch = True
            continue
        resolved.append(ntpath.normpath(base))

    # Windows paths are case-insensitive; the same concrete path can be
    # produced by several placeholder combinations.
    unique = {}  # type: Dict[str, str]
    for path in resolved:
        unique.setdefault(os.path.normcase(path), path)
    resolved = sorted(unique.values(), key=os.path.normcase)

    if resolved:
        status = STATUS_PRESENT
        detail = None
    elif denied:
        status = STATUS_NO_PERMISSION
        detail = DETAIL_PERMISSION_DENIED
        warnings.append(
            Warning(
                code=DETAIL_PERMISSION_DENIED,
                message="access denied while checking %s" % pattern,
                path=pattern,
            )
        )
        if error_detail:
            warnings[-1].message += ": %s" % error_detail
    else:
        status = STATUS_ABSENT
        detail = DETAIL_TYPE_MISMATCH if type_mismatch else DETAIL_NOT_FOUND

    return (
        PathHit(
            template=entry.path,
            pattern=pattern,
            type=entry.type,
            safety=entry.safety,
            reason=entry.reason,
            handling=entry.handling,
            status=status,
            resolved=resolved,
            detail=detail,
        ),
        warnings,
    )


def _aggregate_status(directories):
    # type: (List[PathHit]) -> str
    if any(hit.status == STATUS_PRESENT for hit in directories):
        return STATUS_PRESENT
    if any(hit.status == STATUS_NO_PERMISSION for hit in directories):
        return STATUS_NO_PERMISSION
    return STATUS_ABSENT


def discover(catalog, environ=None):
    # type: (Catalog, Optional[Mapping[str, str]]) -> ScanDiscovery
    """Check every catalog entry and report present/absent/no_permission.

    Every tool in the catalog is reported even when all of its paths are
    missing, so the UI can show "not installed" as a first-class state.
    One broken path never aborts the scan.
    """
    tools = []  # type: List[ToolDiscovery]
    warnings = []  # type: List[Warning]

    for tool in catalog.tools:
        hits = []  # type: List[PathHit]
        for entry in tool.directories:
            hit, entry_warnings = _probe(entry, environ)
            for warning in entry_warnings:
                warning.tool_id = tool.id
            warnings.extend(entry_warnings)
            hits.append(hit)

        tools.append(
            ToolDiscovery(
                id=tool.id,
                name=tool.name,
                kind=tool.kind,
                status=_aggregate_status(hits),
                directories=hits,
                notes=tool.notes,
            )
        )

    return ScanDiscovery(tools=tools, warnings=warnings)
