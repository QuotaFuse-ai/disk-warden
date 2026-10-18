# -*- coding: utf-8 -*-
"""Data models for the catalog-driven scan layer.

The scan layer only reports facts (does the path exist, may we look at it).
Safety classification is owned by the rules layer (T07); this module only
carries the catalog's ``safety``/``reason`` values through for that layer.

Python 3.8 compatible (decision 0005).
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

STATUS_PRESENT = "present"
STATUS_ABSENT = "absent"
STATUS_NO_PERMISSION = "no_permission"

STATUS_VALUES = (STATUS_PRESENT, STATUS_ABSENT, STATUS_NO_PERMISSION)

STATUS_LABELS = {
    STATUS_PRESENT: "已存在",
    STATUS_ABSENT: "不存在",
    STATUS_NO_PERMISSION: "无权限",
}


@dataclass(frozen=True)
class CatalogDirectory:
    """One catalog path entry, exactly as stored in data/ide-catalog.json."""

    path: str
    type: str
    safety: str
    reason: str
    handling: Optional[str] = None


@dataclass(frozen=True)
class CatalogTool:
    id: str
    name: str
    kind: str
    platform: str
    processes: Tuple[str, ...]
    directories: Tuple[CatalogDirectory, ...]
    notes: Optional[str] = None


@dataclass(frozen=True)
class Catalog:
    schema_version: int
    source: str
    platform: str
    updated_at: str
    tools: Tuple[CatalogTool, ...]


@dataclass
class PathHit:
    """Result of probing one catalog directory entry.

    ``pattern`` is the environment-expanded template (wildcards preserved),
    ``resolved`` holds the concrete paths that actually exist.
    """

    template: str
    pattern: str
    type: str
    safety: str
    reason: str
    handling: Optional[str]
    status: str
    resolved: List[str] = field(default_factory=list)
    detail: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        data = {
            "template": self.template,
            "pattern": self.pattern,
            "type": self.type,
            "safety": self.safety,
            "reason": self.reason,
            "status": self.status,
            "status_label": STATUS_LABELS[self.status],
            "resolved": list(self.resolved),
        }
        if self.handling is not None:
            data["handling"] = self.handling
        if self.detail is not None:
            data["detail"] = self.detail
        return data


@dataclass
class Warning:
    code: str
    message: str
    tool_id: Optional[str] = None
    path: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        data = {"code": self.code, "message": self.message}
        if self.tool_id is not None:
            data["tool_id"] = self.tool_id
        if self.path is not None:
            data["path"] = self.path
        return data


@dataclass
class ToolDiscovery:
    id: str
    name: str
    kind: str
    status: str
    directories: List[PathHit] = field(default_factory=list)
    notes: Optional[str] = None

    def counts(self) -> Dict[str, int]:
        counts = {"total": len(self.directories)}
        for status in STATUS_VALUES:
            counts[status] = sum(
                1 for hit in self.directories if hit.status == status
            )
        return counts

    def to_dict(self) -> Dict[str, object]:
        data = {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "status": self.status,
            "status_label": STATUS_LABELS[self.status],
            "counts": self.counts(),
            "directories": [hit.to_dict() for hit in self.directories],
        }
        if self.notes is not None:
            data["notes"] = self.notes
        return data


@dataclass
class ScanDiscovery:
    """Read-only result of the catalog existence scan."""

    tools: List[ToolDiscovery] = field(default_factory=list)
    warnings: List[Warning] = field(default_factory=list)

    def summary(self) -> Dict[str, Dict[str, int]]:
        summary = {
            "tools": {"total": len(self.tools)},
            "directories": {
                "total": sum(len(tool.directories) for tool in self.tools)
            },
        }
        for status in STATUS_VALUES:
            summary["tools"][status] = sum(
                1 for tool in self.tools if tool.status == status
            )
            summary["directories"][status] = sum(
                1
                for tool in self.tools
                for hit in tool.directories
                if hit.status == status
            )
        summary["warnings"] = {"total": len(self.warnings)}
        return summary

    def to_dict(self) -> Dict[str, object]:
        return {
            "summary": self.summary(),
            "tools": [tool.to_dict() for tool in self.tools],
            "warnings": [warning.to_dict() for warning in self.warnings],
        }
