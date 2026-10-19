# -*- coding: utf-8 -*-
"""Minimal, dependency-free loader for ``data/ide-catalog.json``.

The catalog is produced and validated by T03 (``tools/validate_rules.py``).
This loader only enforces the parts the scan layer depends on, so a corrupt
catalog fails loudly at startup instead of silently skipping tools.

Python 3.8 compatible (decision 0005).
"""

import json
from typing import Any, Dict, List, Optional

from .models import Catalog, CatalogDirectory, CatalogTool

TYPE_VALUES = ("dir", "file", "glob")
SAFETY_VALUES = ("green", "yellow", "red")
HANDLING_VALUES = ("junction", "env-var", "app-config", "docker-settings")


class CatalogError(ValueError):
    """Raised when the catalog cannot be parsed or violates its schema."""


def _require_string(mapping, key, where):
    # type: (Dict[str, Any], str, str) -> str
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CatalogError("%s.%s: must be a non-empty string" % (where, key))
    return value


def _optional_string(mapping, key, where):
    # type: (Dict[str, Any], str, str) -> Optional[str]
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise CatalogError("%s.%s: must be a string" % (where, key))
    return value


def _parse_directory(raw, where):
    # type: (Any, str) -> CatalogDirectory
    if not isinstance(raw, dict):
        raise CatalogError("%s: must be an object" % where)

    path = _require_string(raw, "path", where)
    entry_type = raw.get("type")
    if entry_type not in TYPE_VALUES:
        raise CatalogError(
            "%s.type: must be one of %s" % (where, ", ".join(TYPE_VALUES))
        )
    safety = raw.get("safety")
    if safety not in SAFETY_VALUES:
        raise CatalogError(
            "%s.safety: must be one of %s" % (where, ", ".join(SAFETY_VALUES))
        )
    reason = _require_string(raw, "reason", where)

    handling = raw.get("handling")
    if handling is not None and handling not in HANDLING_VALUES:
        raise CatalogError(
            "%s.handling: must be one of %s"
            % (where, ", ".join(HANDLING_VALUES))
        )

    return CatalogDirectory(
        path=path,
        type=entry_type,
        safety=safety,
        reason=reason,
        handling=handling,
    )


def _parse_tool(raw, where):
    # type: (Any, str) -> CatalogTool
    if not isinstance(raw, dict):
        raise CatalogError("%s: must be an object" % where)

    tool_id = _require_string(raw, "id", where)
    name = _require_string(raw, "name", where)
    kind = _require_string(raw, "kind", where)
    platform = _require_string(raw, "platform", where)

    processes = raw.get("processes", [])
    if not isinstance(processes, list) or not all(
        isinstance(item, str) for item in processes
    ):
        raise CatalogError("%s.processes: must be an array of strings" % where)

    directories = raw.get("directories")
    if not isinstance(directories, list) or not directories:
        raise CatalogError("%s.directories: must be a non-empty array" % where)

    parsed_directories = [
        _parse_directory(entry, "%s.directories[%d]" % (where, index))
        for index, entry in enumerate(directories)
    ]

    return CatalogTool(
        id=tool_id,
        name=name,
        kind=kind,
        platform=platform,
        processes=tuple(processes),
        directories=tuple(parsed_directories),
        notes=_optional_string(raw, "notes", where),
    )


def parse_catalog(raw):
    # type: (Any) -> Catalog
    """Build a ``Catalog`` from already-decoded JSON data."""
    if not isinstance(raw, dict):
        raise CatalogError("catalog root must be an object")

    schema_version = raw.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise CatalogError("catalog.schema_version: must be an integer")

    entries = raw.get("ides")
    if not isinstance(entries, list) or not entries:
        raise CatalogError("catalog.ides: must be a non-empty array")

    tools = []  # type: List[CatalogTool]
    seen_ids = set()
    for index, entry in enumerate(entries):
        tool = _parse_tool(entry, "ides[%d]" % index)
        if tool.id in seen_ids:
            raise CatalogError("ides[%d].id: duplicate id %r" % (index, tool.id))
        seen_ids.add(tool.id)
        tools.append(tool)

    return Catalog(
        schema_version=schema_version,
        source=str(raw.get("source", "")),
        platform=str(raw.get("platform", "")),
        updated_at=str(raw.get("updated_at", "")),
        tools=tuple(tools),
    )


def load_catalog(path):
    # type: (Any) -> Catalog
    """Read and parse the catalog from ``path`` (UTF-8, no BOM expected)."""
    try:
        with open(str(path), "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except OSError as exc:
        raise CatalogError("cannot read catalog %s: %s" % (path, exc))
    except ValueError as exc:
        raise CatalogError("invalid JSON in catalog %s: %s" % (path, exc))
    return parse_catalog(raw)
