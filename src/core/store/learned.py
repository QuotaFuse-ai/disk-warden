# -*- coding: utf-8 -*-
"""Persisted learned rules (requirements 2.4 / AC-STORE-03).

Learned rules remember the user's decision for a scanned item.  Only ``red``
and ``green`` exist: red is final, green may turn a yellow name-rule verdict
green but can never weaken red.  The file shape is directly consumable by
``src.core.rules.ClassifyEngine`` (``{item_id: {safety, reason, rule_id}}``).

A missing or damaged file yields an empty rule set plus a warning; the rules
engine then falls back to name rules and unknown/yellow, never to green.

Python 3.8 compatible (decision 0005).
"""

import os
from typing import Dict, List, Optional

from .atomic import (
    StoreCorruptError,
    StoreError,
    atomic_write_json,
    quarantine_corrupt,
    read_json_file,
    utc_now,
)

SCHEMA_VERSION = 1
SAFETY_GREEN = "green"
SAFETY_RED = "red"
LEARNED_SAFETY_VALUES = (SAFETY_GREEN, SAFETY_RED)
DEFAULT_RULE_ID = "learned"


class LearnedRuleError(StoreError):
    """Raised when a learned-rule change would be unsafe or invalid."""


def _coerce_item_id(item_id):
    # type: (object) -> str
    if not isinstance(item_id, str) or not item_id.strip():
        raise LearnedRuleError("learned rule needs a non-empty item_id")
    return item_id


def _normalise_rule(raw):
    # type: (object) -> Optional[Dict[str, str]]
    if isinstance(raw, str):
        safety = raw
        reason = ""
        rule_id = DEFAULT_RULE_ID
    elif isinstance(raw, dict):
        safety = raw.get("safety")
        reason = raw.get("reason") or ""
        rule_id = raw.get("rule_id") or DEFAULT_RULE_ID
    else:
        return None
    if safety not in LEARNED_SAFETY_VALUES:
        return None
    if not isinstance(reason, str) or not isinstance(rule_id, str):
        return None
    return {"safety": safety, "reason": reason, "rule_id": rule_id}


class LearnedRulesStore(object):
    """``<config>/rules-learned.json``."""

    def __init__(self, path):
        # type: (str) -> None
        if not path:
            raise StoreError("learned-rules path is required")
        self.path = os.path.abspath(os.path.normpath(path))
        self.warnings = []  # type: List[Dict[str, object]]
        self.complete = True

    def load(self):
        # type: () -> Dict[str, Dict[str, str]]
        """Return ``item_id -> rule``; never raises for a damaged file."""
        self.warnings = []
        self.complete = True
        try:
            payload = read_json_file(self.path)
        except FileNotFoundError:
            self.complete = False
            self.warnings.append(
                {
                    "code": "learned_missing",
                    "message": "learned rules file is missing",
                }
            )
            return {}
        except StoreCorruptError as exc:
            self.complete = False
            quarantined = quarantine_corrupt(self.path)
            self.warnings.append(
                {
                    "code": "learned_corrupt",
                    "message": str(exc),
                    "quarantined": quarantined,
                }
            )
            return {}

        raw_rules = payload.get("rules") if isinstance(payload, dict) else None
        if raw_rules is None and isinstance(payload, dict):
            # Accept the bare mapping form as well.
            raw_rules = payload
        if not isinstance(raw_rules, dict):
            self.complete = False
            quarantined = quarantine_corrupt(self.path)
            self.warnings.append(
                {
                    "code": "learned_corrupt",
                    "message": "learned rules must be a mapping",
                    "quarantined": quarantined,
                }
            )
            return {}

        rules = {}
        invalid = 0
        for item_id, raw in raw_rules.items():
            if not isinstance(item_id, str) or not item_id.strip():
                invalid += 1
                continue
            rule = _normalise_rule(raw)
            if rule is None:
                invalid += 1
                continue
            rules[item_id] = rule
        if invalid:
            # Valid rules stay usable (AC-STORE-03).
            self.complete = False
            self.warnings.append(
                {
                    "code": "learned_rule_invalid",
                    "message": "%d learned rule(s) were unusable" % invalid,
                }
            )
        return rules

    def save(self, rules):
        # type: (Dict[str, object]) -> Dict[str, Dict[str, str]]
        """Validate and atomically persist a rule mapping."""
        if not isinstance(rules, dict):
            raise LearnedRuleError("learned rules must be a mapping")
        normalised = {}
        for item_id, raw in rules.items():
            key = _coerce_item_id(item_id)
            rule = _normalise_rule(raw)
            if rule is None:
                raise LearnedRuleError(
                    "learned rule for %r must be green or red" % (item_id,)
                )
            normalised[key] = rule
        atomic_write_json(
            self.path,
            {
                "schema_version": SCHEMA_VERSION,
                "updated_at": utc_now(),
                "rules": normalised,
            },
        )
        return normalised

    def get(self, item_id):
        # type: (str) -> Optional[Dict[str, str]]
        return self.load().get(item_id)

    def set_rule(self, item_id, safety, reason="", rule_id=DEFAULT_RULE_ID, now=None):
        # type: (str, str, str, str, object) -> bool
        """Set one rule; returns False when the existing rule already wins.

        Red is terminal (requirements 2.4): a red rule is never replaced by a
        green one, and no write happens when nothing changes.
        """
        key = _coerce_item_id(item_id)
        if safety not in LEARNED_SAFETY_VALUES:
            raise LearnedRuleError("safety must be green or red: %r" % (safety,))
        rules = self.load()
        current = rules.get(key)
        if current is not None:
            if current.get("safety") == SAFETY_RED and safety == SAFETY_GREEN:
                return False
            if (
                current.get("safety") == safety
                and current.get("reason", "") == (reason or "")
                and current.get("rule_id", DEFAULT_RULE_ID)
                == (rule_id or DEFAULT_RULE_ID)
            ):
                return False

        rules[key] = {
            "safety": safety,
            "reason": reason or "",
            "rule_id": rule_id or DEFAULT_RULE_ID,
        }
        atomic_write_json(
            self.path,
            {
                "schema_version": SCHEMA_VERSION,
                "updated_at": utc_now(now),
                "rules": rules,
            },
        )
        return True

    def mark_red(self, item_id, reason="", rule_id=DEFAULT_RULE_ID, now=None):
        # type: (str, str, str, object) -> bool
        return self.set_rule(item_id, SAFETY_RED, reason, rule_id, now=now)

    def mark_green(self, item_id, reason="", rule_id=DEFAULT_RULE_ID, now=None):
        # type: (str, str, str, object) -> bool
        return self.set_rule(item_id, SAFETY_GREEN, reason, rule_id, now=now)

    def remove(self, item_id):
        # type: (str) -> bool
        """Remove one rule (used when the user re-classifies an item)."""
        key = _coerce_item_id(item_id)
        rules = self.load()
        if key not in rules:
            return False
        rules.pop(key)
        self.save(rules)
        return True


__all__ = [
    "DEFAULT_RULE_ID",
    "LEARNED_SAFETY_VALUES",
    "LearnedRuleError",
    "LearnedRulesStore",
    "SAFETY_GREEN",
    "SAFETY_RED",
    "SCHEMA_VERSION",
]
