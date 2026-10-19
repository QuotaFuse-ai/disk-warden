# -*- coding: utf-8 -*-
"""Pure classification engine (scan layer 2).

Priority (architecture §4.3):

1. catalog exact hit (tool id + expanded path);
2. learned decisions (injected, never stored here);
3. name/pattern rules from ``data/rules.json``;
4. unknown -> yellow, with an optional AI resolver interface (offline).

Red is terminal: no learned record, name rule or AI suggestion can turn a red
item into anything else.  Yellow always requires explicit confirmation, and
the only automatic migration candidates are green items that do not require
confirmation.

The engine performs no filesystem or storage access; every input is injected.

Python 3.8 compatible (decision 0005).
"""

import hashlib
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence, Tuple

from .catalog_index import KIND_DIR, CatalogIndex, normalize_path
from .loader import RulesError
from .models import (
    SAFETY_GREEN,
    SAFETY_RED,
    SAFETY_YELLOW,
    SOURCE_AI,
    SOURCE_CATALOG,
    SOURCE_LEARNED,
    Classification,
)
from .name_rules import NameRules, NameVerdict, basename

LEARNED_SAFETY_VALUES = (SAFETY_GREEN, SAFETY_RED)
DEFAULT_LEARNED_REASON = "Learned from previous user decisions"
DEFAULT_AI_REASON_KEY = "rule.reason.ai"


@dataclass(frozen=True)
class LearnedRule:
    """A decision learned from earlier user actions.

    Learned data lives outside this layer (the store injects it).  It may only
    escalate to red or resolve yellow to green/red; it can never weaken red.
    """

    item_id: str
    safety: str
    reason: str = DEFAULT_LEARNED_REASON
    rule_id: str = "learned"


def _coerce_learned(records):
    # type: (Optional[object]) -> dict
    if records is None:
        return {}
    result = {}
    if isinstance(records, Mapping):
        source = records.items()
    elif isinstance(records, Sequence) and not isinstance(records, (str, bytes)):
        source = []
        for record in records:
            if isinstance(record, LearnedRule):
                source.append((record.item_id, record))
            elif isinstance(record, Mapping):
                item_id = record.get("item_id")
                source.append((item_id, record))
            else:
                raise RulesError("learned rules must be LearnedRule objects or mappings")
    else:
        raise RulesError("learned rules must be a mapping or a sequence")

    for item_id, record in source:
        if not isinstance(item_id, str) or not item_id.strip():
            raise RulesError("learned rule is missing a non-empty item_id")
        if isinstance(record, LearnedRule):
            rule = record
        elif isinstance(record, str):
            rule = LearnedRule(item_id=item_id, safety=record)
        elif isinstance(record, Mapping):
            rule = LearnedRule(
                item_id=item_id,
                safety=record.get("safety"),
                reason=record.get("reason") or DEFAULT_LEARNED_REASON,
                rule_id=record.get("rule_id") or "learned",
            )
        else:
            raise RulesError("learned rule for %r has an unsupported value" % item_id)
        if rule.safety not in LEARNED_SAFETY_VALUES:
            raise RulesError(
                "learned rule for %r must be green or red, got %r"
                % (item_id, rule.safety)
            )
        result[item_id] = rule
    return result


def item_id_for(tool_id, source_path=None, rel_path=None):
    # type: (str, Optional[str], Optional[str]) -> str
    """Stable item id: ``sha256(tool_id + "\\n" + normalized relative path)``."""
    raw = rel_path if rel_path is not None else (source_path or "")
    key = "%s\n%s" % (str(tool_id).casefold(), normalize_path(raw).casefold())
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _requires_confirm(safety):
    # type: (str) -> bool
    return safety == SAFETY_YELLOW


def _get(item, attribute, default=None):
    # type: (object, str, object) -> object
    if isinstance(item, Mapping):
        return item.get(attribute, default)
    return getattr(item, attribute, default)


class ClassifyEngine(object):
    """Stateless classifier combining catalog, learned data and name rules."""

    def __init__(self, name_rules, catalog_index=None, learned=None, ai_resolver=None):
        # type: (NameRules, Optional[CatalogIndex], Optional[object], Optional[object]) -> None
        if not isinstance(name_rules, NameRules):
            raise TypeError("name_rules must be a NameRules instance")
        if catalog_index is not None and not isinstance(catalog_index, CatalogIndex):
            raise TypeError("catalog_index must be a CatalogIndex instance")
        self.name_rules = name_rules
        self.catalog_index = catalog_index
        self.learned = _coerce_learned(learned)
        self.ai_resolver = ai_resolver

    def classify_name(self, name):
        # type: (str) -> NameVerdict
        """Expose bare name matching (used by diagnostics and tests)."""
        return self.name_rules.classify_name(name)

    def classify(self, item):
        # type: (object) -> Classification
        item_id = _get(item, "item_id") or item_id_for(
            _get(item, "tool_id", ""),
            source_path=_get(item, "source_path"),
            rel_path=_get(item, "rel_path"),
        )
        kind = _get(item, "kind", KIND_DIR) or KIND_DIR
        source_path = _get(item, "source_path", "") or ""
        rel_path = _get(item, "rel_path", "") or ""
        name = basename(source_path or rel_path)

        catalog_match = None
        if self.catalog_index is not None:
            catalog_match = self.catalog_index.classify(
                _get(item, "tool_id", ""), source_path, kind
            )

        if catalog_match is not None:
            verdict = NameVerdict(
                safety=catalog_match.safety,
                source=SOURCE_CATALOG,
                rule_id=catalog_match.rule_id,
                reason=catalog_match.reason,
                reason_key=catalog_match.reason_key,
            )
        else:
            verdict = self.name_rules.classify_name(name)

        # Red is terminal: catalog entries and red name rules both stand.
        if verdict.safety == SAFETY_RED:
            return self._build(item_id, verdict, requires_confirm=False)

        learned = self.learned.get(item_id)
        if learned is not None:
            if learned.safety == SAFETY_RED:
                return Classification(
                    item_id=item_id,
                    safety=SAFETY_RED,
                    source=SOURCE_LEARNED,
                    rule_id=learned.rule_id,
                    reason=learned.reason,
                    reason_key="rule.reason.learned",
                    requires_confirm=False,
                )
            if learned.safety == SAFETY_GREEN and verdict.safety == SAFETY_YELLOW:
                return Classification(
                    item_id=item_id,
                    safety=SAFETY_GREEN,
                    source=SOURCE_LEARNED,
                    rule_id=learned.rule_id,
                    reason=learned.reason,
                    reason_key="rule.reason.learned",
                    requires_confirm=False,
                )
            # Learned green on an existing green keeps the original provenance.

        current = self._build(item_id, verdict, requires_confirm=_requires_confirm(verdict.safety))

        if verdict.safety == SAFETY_YELLOW and self.ai_resolver is not None:
            suggestion = self.ai_resolver.classify(item, current)
            if suggestion is not None and suggestion.safety in (SAFETY_GREEN, SAFETY_RED):
                # The AI layer never overrides catalog/pattern red or green, and
                # its verdict always stays behind explicit user confirmation.
                return Classification(
                    item_id=item_id,
                    safety=suggestion.safety,
                    source=SOURCE_AI,
                    rule_id="ai",
                    reason=suggestion.reason,
                    reason_key=suggestion.reason_key or DEFAULT_AI_REASON_KEY,
                    requires_confirm=True,
                )

        return current

    def classify_many(self, items):
        # type: (Sequence[object]) -> Tuple[Classification, ...]
        return tuple(self.classify(item) for item in items)

    def _build(self, item_id, verdict, requires_confirm):
        # type: (str, NameVerdict, bool) -> Classification
        return Classification(
            item_id=item_id,
            safety=verdict.safety,
            source=verdict.source,
            rule_id=verdict.rule_id,
            reason=verdict.reason,
            reason_key=verdict.reason_key,
            requires_confirm=requires_confirm,
        )
