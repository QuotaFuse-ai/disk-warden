# -*- coding: utf-8 -*-
"""Data models for the rules layer (T07).

The rules layer is pure: it receives scan facts plus already-loaded rule data
and returns a ``Classification``.  It never touches the filesystem.

Python 3.8 compatible (decision 0005).
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional

try:  # Python 3.8+
    from typing import Protocol
except ImportError:  # pragma: no cover - only for very old interpreters
    Protocol = object  # type: ignore

SAFETY_GREEN = "green"
SAFETY_YELLOW = "yellow"
SAFETY_RED = "red"
SAFETY_VALUES = (SAFETY_GREEN, SAFETY_YELLOW, SAFETY_RED)

SOURCE_CATALOG = "catalog"
SOURCE_LEARNED = "learned"
SOURCE_PATTERN = "pattern"
SOURCE_UNKNOWN = "unknown"
SOURCE_AI = "ai"
SOURCE_VALUES = (
    SOURCE_CATALOG,
    SOURCE_LEARNED,
    SOURCE_PATTERN,
    SOURCE_UNKNOWN,
    SOURCE_AI,
)

KIND_DIR = "dir"
KIND_FILE = "file"
KIND_VALUES = (KIND_DIR, KIND_FILE)

# Fixed identities for the "no rule matched" and injected-override results.
UNKNOWN_RULE_ID = "unknown"
UNKNOWN_REASON_KEY = "rule.reason.unknown"


@dataclass(frozen=True)
class RuleItem:
    """Minimal view of a scan item accepted by the rules layer.

    T08's ``ScanItem`` already carries these fields; the engine accepts any
    object with the same attributes, so no cross-layer model needs to be
    imported here.
    """

    tool_id: str
    source_path: str
    kind: str = KIND_DIR
    item_id: Optional[str] = None
    rel_path: Optional[str] = None


@dataclass(frozen=True)
class AISuggestion:
    """Result of the optional third-layer (AI) resolver.

    The AI layer is an interface only in this release; no network client is
    shipped.  A resolver must not be trusted more than the red safety rules.
    """

    safety: str
    reason: str = "AI suggestion (offline interface, not enabled in this release)"
    reason_key: str = "rule.reason.ai"


class AIResolver(Protocol):
    """Optional third-layer resolver injected by the app layer."""

    def classify(self, item, current):  # type: (Any, "Classification") -> Optional[AISuggestion]
        ...


@dataclass(frozen=True)
class Classification:
    """Safety verdict for one item, per architecture §4.1."""

    item_id: str
    safety: str
    source: str
    rule_id: str
    reason: str
    reason_key: str
    requires_confirm: bool

    @property
    def can_migrate(self):
        # type: () -> bool
        """Only green verdicts without an extra confirmation may be migrated."""
        return self.safety == SAFETY_GREEN and not self.requires_confirm

    def to_dict(self):
        # type: () -> Dict[str, object]
        return {
            "item_id": self.item_id,
            "safety": self.safety,
            "source": self.source,
            "rule_id": self.rule_id,
            "reason": self.reason,
            "reason_key": self.reason_key,
            "requires_confirm": self.requires_confirm,
            "can_migrate": self.can_migrate,
        }


@dataclass(frozen=True)
class RuleWarning:
    """Non-fatal problem while preparing rule data (e.g. missing env var)."""

    code: str
    message: str
    path: Optional[str] = None

    def to_dict(self):
        # type: () -> Dict[str, object]
        data = {"code": self.code, "message": self.message}
        if self.path is not None:
            data["path"] = self.path
        return data
