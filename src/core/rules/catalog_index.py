# -*- coding: utf-8 -*-
"""Catalog (layer 1) matching for the rules layer.

The catalog is the authoritative, hand-curated list from
``data/ide-catalog.json``.  Matching happens against the *expanded* absolute
path of a scan item, scoped by tool id, without touching the filesystem.

Path semantics mirror ``tools/validate_rules.py``:

* ``<name>`` segments match exactly one non-empty segment;
* ``*`` / ``?`` appear only in the last segment of ``glob`` entries;
* comparison is case-insensitive; ``/`` and ``\\`` are equivalent.

This module is pure: the caller injects an environment mapping and the parsed
catalog.  No I/O.

Python 3.8 compatible (decision 0005).
"""

import fnmatch
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .models import (
    SAFETY_GREEN,
    SAFETY_RED,
    SAFETY_YELLOW,
    RuleWarning,
)

SEGMENT_PLACEHOLDER_RE = re.compile(r"^(?:<[^<>\s]+>)+$")
PLACEHOLDER_TOKEN_RE = re.compile(r"<[^<>\s]+>")
CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f]")
TRAILING_NOISE = " ."

SAFETY_PRIORITY = {SAFETY_GREEN: 1, SAFETY_YELLOW: 0, SAFETY_RED: 2}

KIND_DIR = "dir"
KIND_FILE = "file"


def split_path(path):
    # type: (str) -> Tuple[str, ...]
    """Split a Windows path into normalized comparison segments."""
    if not isinstance(path, str):
        return ()
    text = path.replace("/", "\\")
    segments = []
    for segment in text.split("\\"):
        if not segment:
            continue
        if CONTROL_CHARS_RE.search(segment):
            continue
        segments.append(segment.rstrip(TRAILING_NOISE).casefold())
    return tuple(segments)


def normalize_path(path):
    # type: (str) -> str
    """Return the canonical comparison form used for item ids and matching."""
    return "\\".join(split_path(path))


@dataclass(frozen=True)
class TemplateSegment:
    """One segment of a catalog template."""

    kind: str  # literal | placeholder | glob
    value: str  # literal name, regex source, or glob pattern (all casefolded)
    regex: Optional[re.Pattern] = None

    def matches(self, actual):
        # type: (str) -> bool
        if self.kind == "literal":
            return actual == self.value
        if self.kind == "glob":
            return fnmatch.fnmatchcase(actual, self.value)
        if self.regex is not None:
            return self.regex.fullmatch(actual) is not None
        return False


@dataclass(frozen=True)
class CatalogMatch:
    """A successful catalog hit."""

    tool_id: str
    safety: str
    reason: str
    reason_key: str
    rule_id: str
    template: str
    path: str
    kind: str
    handling: Optional[str] = None


@dataclass(frozen=True)
class CatalogEntry:
    """One catalog directory entry, compiled for path matching."""

    tool_id: str
    template: str
    path: str
    kind: str
    safety: str
    reason: str
    reason_key: str
    rule_id: str
    handling: Optional[str]
    segments: Tuple[TemplateSegment, ...]
    literal_count: int
    is_glob: bool
    index: int

    @property
    def rank(self):
        # type: () -> Tuple[int, int, int, int, int, int]
        """Higher is more specific; red breaks ties in the same scope."""
        return (
            self.literal_count,
            len(self.segments),
            0 if self.is_glob else 1,
            len(self.template),
            SAFETY_PRIORITY.get(self.safety, 0),
            -self.index,
        )

    def matches(self, actual_segments):
        # type: (Tuple[str, ...]) -> bool
        if len(actual_segments) != len(self.segments):
            return False
        return all(
            template.matches(actual)
            for template, actual in zip(self.segments, actual_segments)
        )


def _compile_placeholder(segment):
    # type: (str) -> TemplateSegment
    pieces = []
    position = 0
    for match in PLACEHOLDER_TOKEN_RE.finditer(segment):
        literal = segment[position:match.start()]
        if literal:
            pieces.append(re.escape(literal.casefold()))
        pieces.append(".+")
        position = match.end()
    tail = segment[position:]
    if tail:
        pieces.append(re.escape(tail.casefold()))
    source = "".join(pieces)
    return TemplateSegment(
        "placeholder", source, re.compile("^" + source + "$", re.IGNORECASE)
    )


def compile_template(path, entry_type):
    # type: (str, str) -> Tuple[TemplateSegment, ...]
    """Compile an expanded catalog path into matchable segments."""
    raw_segments = [part for part in path.replace("/", "\\").split("\\") if part]
    compiled = []  # type: List[TemplateSegment]
    last_index = len(raw_segments) - 1
    for index, segment in enumerate(raw_segments):
        is_last = index == last_index
        if SEGMENT_PLACEHOLDER_RE.match(segment):
            compiled.append(_compile_placeholder(segment))
        elif is_last and entry_type == "glob" and ("*" in segment or "?" in segment):
            compiled.append(TemplateSegment("glob", segment.casefold()))
        else:
            compiled.append(
                TemplateSegment("literal", segment.rstrip(TRAILING_NOISE).casefold())
            )
    return tuple(compiled)


def _reason_key_for(tool_id, safety, index):
    # type: (str, str, int) -> str
    return "catalog.%s.%d.%s" % (tool_id, index, safety)


def build_entry(tool_id, template, entry, index):
    # type: (str, str, dict, int) -> Optional[CatalogEntry]
    entry_type = entry["type"]
    item_kind = KIND_DIR if entry_type == "dir" else KIND_FILE
    segments = compile_template(template, entry_type)
    if not segments:
        return None
    literal_count = sum(1 for segment in segments if segment.kind == "literal")
    safety = entry["safety"]
    return CatalogEntry(
        tool_id=tool_id,
        template=template,
        path=normalize_path(template),
        kind=item_kind,
        safety=safety,
        reason=entry["reason"],
        reason_key=_reason_key_for(tool_id, safety, index),
        rule_id="catalog.%s.%d" % (tool_id, index),
        handling=entry.get("handling"),
        segments=segments,
        literal_count=literal_count,
        is_glob=entry_type == "glob" or any(
            segment.kind == "glob" for segment in segments
        ),
        index=index,
    )


class CatalogIndex(object):
    """Catalog entries grouped by tool id."""

    def __init__(self, entries, warnings=None):
        # type: (List[CatalogEntry], Optional[List[RuleWarning]]) -> None
        grouped = {}  # type: Dict[str, List[CatalogEntry]]
        for entry in entries:
            grouped.setdefault(entry.tool_id, []).append(entry)
        self._by_tool = grouped
        self.warnings = warnings if warnings is not None else []
        self.entries = tuple(entries)

    def __len__(self):
        # type: () -> int
        return len(self.entries)

    def classify(self, tool_id, source_path, kind=KIND_DIR):
        # type: (str, str, str) -> Optional[CatalogMatch]
        candidates = self._by_tool.get(tool_id)
        if not candidates:
            return None
        actual = split_path(source_path)
        if not actual:
            return None
        matches = [
            entry
            for entry in candidates
            if entry.kind == kind and entry.matches(actual)
        ]
        if not matches:
            return None
        best = max(matches, key=lambda entry: entry.rank)
        return CatalogMatch(
            tool_id=best.tool_id,
            safety=best.safety,
            reason=best.reason,
            reason_key=best.reason_key,
            rule_id=best.rule_id,
            template=best.template,
            path=best.path,
            kind=best.kind,
            handling=best.handling,
        )


def build_warning(code, message, path=None):
    # type: (str, str, Optional[str]) -> RuleWarning
    return RuleWarning(code=code, message=message, path=path)
