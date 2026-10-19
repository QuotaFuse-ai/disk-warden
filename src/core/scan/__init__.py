# -*- coding: utf-8 -*-
"""Catalog-driven scan layer (T06 discovery + T08 measurement).

Public API:
    load_catalog(path) -> Catalog
    discover(catalog, environ=None) -> ScanDiscovery
    expand_environment(template, environ=None) -> str
    measure(targets, cancel_token=None, progress_cb=None) -> MeasureReport
    targets_from_discovery(discovery) -> list[MeasureTarget]
    detect_processes(tools, process_provider=None) -> dict[tool_id, ProcessHit]
"""

from .cancel import CancelToken, ScanCancelled, check_cancelled, is_cancelled
from .catalog import CatalogError, load_catalog, parse_catalog
from .discovery import discover
from .env import (
    UnknownEnvironmentVariable,
    expand_environment,
    find_placeholders,
)
from .models import (
    Catalog,
    CatalogDirectory,
    CatalogTool,
    PathHit,
    ScanDiscovery,
    STATUS_ABSENT,
    STATUS_LABELS,
    STATUS_NO_PERMISSION,
    STATUS_PRESENT,
    STATUS_VALUES,
    ToolDiscovery,
    Warning,
)
from .process import (
    STATUS_NO_PROCESS_NAMES,
    STATUS_NOT_RUNNING,
    STATUS_RUNNING,
    STATUS_UNKNOWN,
    ProcessHit,
    blocked_tool_ids,
    detect_processes,
    summarize as summarize_processes,
)
from .size import (
    KIND_DIR,
    KIND_FILE,
    MeasureReport,
    MeasureTarget,
    ScanItem,
    make_item_id,
    measure,
    targets_from_discovery,
)

__all__ = [
    "CancelToken",
    "Catalog",
    "CatalogDirectory",
    "CatalogError",
    "CatalogTool",
    "KIND_DIR",
    "KIND_FILE",
    "MeasureReport",
    "MeasureTarget",
    "PathHit",
    "ProcessHit",
    "STATUS_ABSENT",
    "STATUS_LABELS",
    "STATUS_NO_PERMISSION",
    "STATUS_NO_PROCESS_NAMES",
    "STATUS_NOT_RUNNING",
    "STATUS_PRESENT",
    "STATUS_RUNNING",
    "STATUS_UNKNOWN",
    "STATUS_VALUES",
    "ScanCancelled",
    "ScanDiscovery",
    "ScanItem",
    "ToolDiscovery",
    "UnknownEnvironmentVariable",
    "Warning",
    "blocked_tool_ids",
    "check_cancelled",
    "detect_processes",
    "discover",
    "expand_environment",
    "find_placeholders",
    "is_cancelled",
    "load_catalog",
    "make_item_id",
    "measure",
    "parse_catalog",
    "summarize_processes",
    "targets_from_discovery",
]
