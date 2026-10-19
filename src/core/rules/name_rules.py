# -*- coding: utf-8 -*-
"""Basename matching for the rules layer.

Matching follows the semantics documented in ``data/README.md`` and enforced by
``tools/validate_rules.py``:

* only the last path segment is considered (both ``\\`` and ``/`` accepted);
* comparison is case-insensitive;
* Windows ignores trailing spaces and dots in names, internal spaces stay
  significant (``Code Cache`` is not ``CodeCache``);
* all matching rules are evaluated and red wins over green and yellow.

This module is pure: no filesystem access, no I/O.

Python 3.8 compatible (decision 0005).
"""

import fnmatch
import re
from dataclasses import dataclass
from typing import Optional, Pattern, Tuple

from .models import (
    SAFETY_GREEN,
    SAFETY_RED,
    SAFETY_YELLOW,
    SOURCE_UNKNOWN,
    UNKNOWN_REASON_KEY,
    UNKNOWN_RULE_ID,
)

SEPARATORS = "\\/"

# Windows ignores trailing spaces and dots when resolving a name.
TRAILING_NOISE = " ."


def basename(path):
    # type: (str) -> str
    """Return the last segment of ``path``, accepting both separators."""
    if not isinstance(path, str):
        return ""
    text = path
    while len(text) > 1 and text[-1] in SEPARATORS:
        text = text[:-1]
    index = max(text.rfind("\\"), text.rfind("/"))
    return text[index + 1:] if index >= 0 else text


def normalize_name(name):
    # type: (str) -> str
    """Return the comparison form of a file/directory name."""
    return basename(name).rstrip(TRAILING_NOISE).casefold()


class NameRule(object):
    """One compiled rule from ``data/rules.json``."""

    __slots__ = (
        "match",
        "match_type",
        "safety",
        "reason",
        "rule_id",
        "reason_key",
        "pattern",
        "regex",
        "index",
    )

    def __init__(self, match, match_type, safety, reason, rule_id, reason_key, index):
        # type: (str, str, str, str, str, str, int) -> None
        self.match = match
        self.match_type = match_type
        self.safety = safety
        self.reason = reason
        self.rule_id = rule_id
        self.reason_key = reason_key
        self.pattern = match.casefold()
        self.regex = None  # type: Optional[Pattern]
        if match_type == "regex":
            self.regex = re.compile(match, re.IGNORECASE)
        self.index = index

    def matches(self, normalized_name):
        # type: (str) -> bool
        if self.match_type == "exact":
            return normalized_name == self.pattern
        if self.match_type == "glob":
            return fnmatch.fnmatchcase(normalized_name, self.pattern)
        if self.match_type == "regex" and self.regex is not None:
            return self.regex.fullmatch(normalized_name) is not None
        return False


@dataclass(frozen=True)
class NameVerdict:
    """Outcome of basename matching, before catalog/learned/AI layering."""

    safety: str
    source: str
    rule_id: str
    reason: str
    reason_key: str


class NameRules(object):
    """Compiled name rules plus the configured fallback for unknown names."""

    def __init__(self, rules, default_safety, default_reason, default_reason_key):
        # type: (Tuple[NameRule, ...], str, str, str) -> None
        self.rules = tuple(rules)
        self.default_safety = default_safety
        self.default_reason = default_reason
        self.default_reason_key = default_reason_key

    def __len__(self):
        # type: () -> int
        return len(self.rules)

    def classify_name(self, name):
        # type: (str) -> NameVerdict
        normalized = normalize_name(name)
        matched = [rule for rule in self.rules if rule.matches(normalized)]  # type: list
        if not matched:
            return NameVerdict(
                self.default_safety,
                SOURCE_UNKNOWN,
                UNKNOWN_RULE_ID,
                self.default_reason,
                self.default_reason_key or UNKNOWN_REASON_KEY,
            )

        # conflict_resolution: red_wins (see data/README.md).
        red = [rule for rule in matched if rule.safety == SAFETY_RED]
        if red:
            chosen = red[0]
            return NameVerdict(
                SAFETY_RED, "pattern", chosen.rule_id, chosen.reason, chosen.reason_key
            )
        green = [rule for rule in matched if rule.safety == SAFETY_GREEN]
        if green:
            chosen = green[0]
            return NameVerdict(
                SAFETY_GREEN, "pattern", chosen.rule_id, chosen.reason, chosen.reason_key
            )
        yellow = [rule for rule in matched if rule.safety == SAFETY_YELLOW]
        chosen = yellow[0]
        return NameVerdict(
            SAFETY_YELLOW, "pattern", chosen.rule_id, chosen.reason, chosen.reason_key
        )
