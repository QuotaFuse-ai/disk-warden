# -*- coding: utf-8 -*-
"""Rules layer (T07): catalog -> learned -> name rules -> unknown.

Public API:
    load_name_rules(path) / parse_name_rules(data)
    load_catalog(path, environ) / parse_catalog(data, environ)
    load_engine(rules_path, catalog_path, environ, learned, ai_resolver)
    ClassifyEngine(name_rules, catalog_index, learned, ai_resolver)
    ClassifyEngine.classify(item) -> Classification
    RuleItem, LearnedRule, Classification, RuleWarning

The classification itself is pure; ``load_*`` helpers are the only I/O.
Python 3.8 compatible (decision 0005).
"""

from .catalog_index import CatalogIndex, CatalogMatch
from .engine import ClassifyEngine, LearnedRule, item_id_for
from .loader import (
    RulesError,
    load_catalog,
    load_engine,
    load_name_rules,
    parse_catalog,
    parse_name_rules,
)
from .models import (
    AIResolver,
    AISuggestion,
    Classification,
    KIND_DIR,
    KIND_FILE,
    KIND_VALUES,
    RuleItem,
    RuleWarning,
    SAFETY_GREEN,
    SAFETY_RED,
    SAFETY_VALUES,
    SAFETY_YELLOW,
    SOURCE_AI,
    SOURCE_CATALOG,
    SOURCE_LEARNED,
    SOURCE_PATTERN,
    SOURCE_UNKNOWN,
    SOURCE_VALUES,
    UNKNOWN_REASON_KEY,
    UNKNOWN_RULE_ID,
)
from .name_rules import NameRule, NameRules, NameVerdict, basename, normalize_name

__all__ = [
    "AIResolver",
    "AISuggestion",
    "CatalogIndex",
    "CatalogMatch",
    "Classification",
    "ClassifyEngine",
    "KIND_DIR",
    "KIND_FILE",
    "KIND_VALUES",
    "LearnedRule",
    "NameRule",
    "NameRules",
    "NameVerdict",
    "RuleItem",
    "RuleWarning",
    "RulesError",
    "SAFETY_GREEN",
    "SAFETY_RED",
    "SAFETY_VALUES",
    "SAFETY_YELLOW",
    "SOURCE_AI",
    "SOURCE_CATALOG",
    "SOURCE_LEARNED",
    "SOURCE_PATTERN",
    "SOURCE_UNKNOWN",
    "SOURCE_VALUES",
    "UNKNOWN_REASON_KEY",
    "UNKNOWN_RULE_ID",
    "basename",
    "item_id_for",
    "load_catalog",
    "load_engine",
    "load_name_rules",
    "normalize_name",
    "parse_catalog",
    "parse_name_rules",
]
