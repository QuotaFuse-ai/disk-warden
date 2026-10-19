# -*- coding: utf-8 -*-
"""Parsing and loading of rule data (``rules.json`` and the IDE catalog).

The classification engine itself is pure.  These helpers are the thin I/O
boundary: ``parse_*`` validates already-decoded JSON, ``load_*`` reads files.
The engine never opens a file.

Data contract: ``data/README.md`` and ``tools/validate_rules.py``.

Python 3.8 compatible (decision 0005).
"""

import json
import re
from typing import Dict, List, Mapping, Optional

from .catalog_index import CatalogIndex, build_entry
from .models import (
    SAFETY_VALUES,
    UNKNOWN_REASON_KEY,
    RuleWarning,
)
from .name_rules import NameRule, NameRules

ENV_VAR_RE = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%")
SLUG_RE = re.compile(r"[^a-z0-9]+")
MATCH_TYPES = ("exact", "glob", "regex")
CATALOG_TYPE_VALUES = ("dir", "file", "glob")


class RulesError(Exception):
    """Raised when rules or catalog data is structurally invalid."""

    def __init__(self, errors):
        # type: (object) -> None
        if isinstance(errors, str):
            errors = [errors]
        self.errors = list(errors)
        Exception.__init__(self, "; ".join(self.errors))


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RulesError("duplicate JSON object key %r" % key)
        result[key] = value
    return result


def _slug(text):
    # type: (str) -> str
    slug = SLUG_RE.sub("_", text.casefold()).strip("_")
    return slug or "rule"


def _is_text(value, min_length=1):
    # type: (object, int) -> bool
    return isinstance(value, str) and len(value.strip()) >= min_length


def _lookup_environment(environ):
    # type: (Optional[Mapping[str, str]]) -> Dict[str, str]
    source = environ if environ is not None else {}
    return {str(key).upper(): str(value) for key, value in source.items()}


def expand_path(template, lookup):
    # type: (str, Dict[str, str]) -> str
    """Expand ``%VAR%`` tokens; raise ``KeyError`` for unknown variables."""

    def replace(match):
        # type: (re.Match) -> str
        name = match.group(1).upper()
        if name not in lookup:
            raise KeyError(name)
        return lookup[name]

    return ENV_VAR_RE.sub(replace, template)


def _rule_reason_key(match_type, match):
    # type: (str, str) -> str
    return "rule.name.%s.%s" % (match_type, _slug(match))


def parse_name_rules(data):
    # type: (object) -> NameRules
    """Validate and compile a decoded ``rules.json`` document."""
    errors = []  # type: List[str]
    if not isinstance(data, dict):
        raise RulesError("rules: top level must be a JSON object")

    if data.get("schema_version") != 1:
        errors.append("rules: schema_version must be 1")
    if data.get("name_scope") != "basename":
        errors.append("rules: name_scope must be basename")
    if data.get("case_sensitive") is not False:
        errors.append("rules: case_sensitive must be false")
    if data.get("conflict_resolution") != "red_wins":
        errors.append("rules: conflict_resolution must be red_wins")

    default = data.get("default")
    default_safety = "yellow"
    default_reason = ""
    default_reason_key = UNKNOWN_REASON_KEY
    if not isinstance(default, dict):
        errors.append("rules.default: must be an object")
    else:
        if default.get("safety") != "yellow":
            errors.append("rules.default.safety: must be yellow")
        else:
            default_safety = default["safety"]
        reason = default.get("reason")
        if not _is_text(reason, 3):
            errors.append("rules.default.reason: must be a non-empty string")
        else:
            default_reason = reason
        key = default.get("reason_key")
        if isinstance(key, str) and key.strip():
            default_reason_key = key

    raw_rules = data.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        errors.append("rules.rules: must be a non-empty array")
        raw_rules = []

    compiled = []  # type: List[NameRule]
    seen = set()
    for index, raw in enumerate(raw_rules):
        location = "rules[%d]" % index
        if not isinstance(raw, dict):
            errors.append("%s: must be an object" % location)
            continue

        match = raw.get("match")
        match_type = raw.get("match_type")
        safety = raw.get("safety")
        reason = raw.get("reason")

        if not _is_text(match):
            errors.append("%s.match: must be a non-empty string" % location)
            match = None
        else:
            if match != match.strip():
                errors.append("%s.match: must not have leading or trailing whitespace" % location)
            if "\\" in match or "/" in match:
                errors.append("%s.match: must be a file or directory name, not a path" % location)
            if "%" in match:
                errors.append("%s.match: must not contain a placeholder" % location)
            if "<" in match or ">" in match:
                errors.append("%s.match: must not contain angle brackets" % location)

        if match_type not in MATCH_TYPES:
            errors.append(
                "%s.match_type: must be one of %s" % (location, ", ".join(MATCH_TYPES))
            )
        if safety not in SAFETY_VALUES:
            errors.append(
                "%s.safety: must be one of %s" % (location, ", ".join(SAFETY_VALUES))
            )
        if not _is_text(reason, 3):
            errors.append("%s.reason: must be a non-empty string" % location)
            reason = None

        if match is not None and match_type in MATCH_TYPES:
            pattern_ok = True
            if match_type == "exact" and ("*" in match or "?" in match):
                errors.append("%s.match: exact rules must not contain a wildcard" % location)
            if match_type == "glob" and "*" not in match:
                errors.append("%s.match: glob rule contains no wildcard" % location)
            if match_type == "regex":
                try:
                    re.compile(match)
                except re.error as exc:
                    pattern_ok = False
                    errors.append("%s.match: invalid regex: %s" % (location, exc))

            key = (match_type, match.casefold())
            if key in seen:
                errors.append(
                    "%s: duplicate rule for %s %r" % (location, match_type, match)
                )
            seen.add(key)

            if pattern_ok and safety in SAFETY_VALUES and reason:
                compiled.append(
                    NameRule(
                        match=match,
                        match_type=match_type,
                        safety=safety,
                        reason=reason,
                        rule_id="pattern.%s.%s" % (match_type, _slug(match)),
                        reason_key=_rule_reason_key(match_type, match),
                        index=index,
                    )
                )

    if errors:
        raise RulesError(errors)

    return NameRules(
        rules=tuple(compiled),
        default_safety=default_safety,
        default_reason=default_reason,
        default_reason_key=default_reason_key,
    )


def parse_catalog(data, environ=None):
    # type: (object, Optional[Mapping[str, str]]) -> CatalogIndex
    """Validate a decoded ``ide-catalog.json`` and build a matching index.

    Entries whose environment variables cannot be resolved are skipped and
    reported through ``CatalogIndex.warnings``; they must not abort the whole
    scan when only ``%APPDATA%`` and friends are missing.
    """
    if not isinstance(data, dict):
        raise RulesError("catalog: top level must be a JSON object")
    ides = data.get("ides")
    if not isinstance(ides, list):
        raise RulesError("catalog: ides must be an array")

    errors = []  # type: List[str]
    warnings = []  # type: List[RuleWarning]
    lookup = _lookup_environment(environ)
    entries = []

    for ide_index, ide in enumerate(ides):
        location = "ides[%d]" % ide_index
        if not isinstance(ide, dict):
            errors.append("%s: must be an object" % location)
            continue
        tool_id = ide.get("id")
        if not _is_text(tool_id):
            errors.append("%s.id: must be a non-empty string" % location)
            continue
        location = "ides[%d](%s)" % (ide_index, tool_id)

        directories = ide.get("directories")
        if not isinstance(directories, list) or not directories:
            errors.append("%s.directories: must be a non-empty array" % location)
            continue

        for dir_index, raw in enumerate(directories):
            entry_location = "%s.directories[%d]" % (location, dir_index)
            if not isinstance(raw, dict):
                errors.append("%s: must be an object" % entry_location)
                continue
            template = raw.get("path")
            entry_type = raw.get("type")
            safety = raw.get("safety")
            reason = raw.get("reason")
            if not _is_text(template):
                errors.append("%s.path: must be a non-empty string" % entry_location)
                continue
            if entry_type not in CATALOG_TYPE_VALUES:
                errors.append("%s.type: must be one of dir, file, glob" % entry_location)
                continue
            if safety not in SAFETY_VALUES:
                errors.append("%s.safety: must be green, yellow or red" % entry_location)
                continue
            if not _is_text(reason, 3):
                errors.append("%s.reason: must be a non-empty string" % entry_location)
                continue

            try:
                expanded = expand_path(template, lookup)
            except KeyError as exc:
                warnings.append(
                    RuleWarning(
                        code="unresolved_environment",
                        message="environment variable %%%s%% is not set" % exc.args[0],
                        path=template,
                    )
                )
                continue
            if "%" in expanded:
                warnings.append(
                    RuleWarning(
                        code="unresolved_environment",
                        message="path still contains an unresolved placeholder",
                        path=template,
                    )
                )
                continue

            entry = build_entry(tool_id, expanded, raw, dir_index)
            if entry is None:
                warnings.append(
                    RuleWarning(
                        code="empty_catalog_entry",
                        message="catalog entry has no path segments",
                        path=template,
                    )
                )
                continue
            entries.append(entry)

    if errors:
        raise RulesError(errors)
    return CatalogIndex(entries, warnings=warnings)


def _load_json(path):
    # type: (str) -> object
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle, object_pairs_hook=_no_duplicate_keys)
    except RulesError:
        raise
    except json.JSONDecodeError as exc:
        raise RulesError(
            "%s: invalid JSON at line %d column %d: %s"
            % (path, exc.lineno, exc.colno, exc.msg)
        )
    except OSError as exc:
        raise RulesError("%s: cannot read file: %s" % (path, exc))


def load_name_rules(path):
    # type: (str) -> NameRules
    return parse_name_rules(_load_json(path))


def load_catalog(path, environ=None):
    # type: (str, Optional[Mapping[str, str]]) -> CatalogIndex
    return parse_catalog(_load_json(path), environ=environ)


def load_engine(rules_path, catalog_path=None, environ=None, learned=None, ai_resolver=None):
    """Convenience wrapper: read both data files and build a pure engine."""
    from .engine import ClassifyEngine

    name_rules = load_name_rules(rules_path)
    catalog_index = load_catalog(catalog_path, environ=environ) if catalog_path else None
    return ClassifyEngine(
        name_rules,
        catalog_index=catalog_index,
        learned=learned,
        ai_resolver=ai_resolver,
    )


__all__ = [
    "CatalogIndex",
    "RulesError",
    "expand_path",
    "load_catalog",
    "load_engine",
    "load_name_rules",
    "parse_catalog",
    "parse_name_rules",
]
