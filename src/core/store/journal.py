# -*- coding: utf-8 -*-
"""Persistent migration/rollback journal (architecture 6.4 / AC-ROL-04).

The journal is written **before** an action (intent) and again after it
(result), so a crash leaves enough evidence to decide whether a step ran.
Every update replaces the file atomically; a reader therefore never sees a
half-written journal.

A migration record lives at ``<jobs>/<job_id>.json``; a rollback record keeps
the original job id but uses ``<jobs>/<job_id>-rollback.json`` so the
migration record is not overwritten (architecture 6.7).

Python 3.8 compatible (decision 0005).
"""

import os
import re
from dataclasses import dataclass, field
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
TYPE_MIGRATION = "migration"
TYPE_ROLLBACK = "rollback"
TYPE_VALUES = (TYPE_MIGRATION, TYPE_ROLLBACK)

JOB_ID_RE = re.compile(r"^[0-9a-f]{8,32}$")
ROLLBACK_SUFFIX = "-rollback.json"
JOURNAL_SUFFIX = ".json"
# Bookkeeping for banner dismissals; the leading dot keeps it out of the way
# and the name cannot match JOB_ID_RE, so journal scans skip it.
IGNORED_FILENAME = ".ignored.json"

STATE_PLANNED = "PLANNED"
STATE_BACKED_UP = "BACKED_UP"
STATE_COPYING = "COPYING"
STATE_COPIED = "COPIED"
STATE_COPY_VERIFIED = "COPY_VERIFIED"
STATE_TARGET_READY = "TARGET_READY"
STATE_SOURCE_RENAMED = "SOURCE_RENAMED"
STATE_LINKED = "LINKED"
STATE_VERIFIED_LINK = "VERIFIED_LINK"
STATE_LINKED_CLEANUP_PENDING = "LINKED_CLEANUP_PENDING"
STATE_DONE = "DONE"
STATE_FAILED = "FAILED"
STATE_ROLLED_BACK = "ROLLED_BACK"
STATE_RECOVERY_REQUIRED = "RECOVERY_REQUIRED"

STATE_ROLLBACK_PLANNED = "ROLLBACK_PLANNED"
STATE_ROLLBACK_LINK_REMOVED = "ROLLBACK_LINK_REMOVED"
STATE_ROLLBACK_COPYING = "ROLLBACK_COPYING"
STATE_ROLLBACK_COPIED = "ROLLBACK_COPIED"
STATE_ROLLBACK_VERIFIED = "ROLLBACK_VERIFIED"
STATE_ROLLBACK_SOURCE_READY = "ROLLBACK_SOURCE_READY"
STATE_ROLLBACK_CONFIG_RESTORED = "ROLLBACK_CONFIG_RESTORED"
STATE_ROLLBACK_FAILED = "ROLLBACK_FAILED"
STATE_ROLLBACK_RECOVERY_REQUIRED = "ROLLBACK_RECOVERY_REQUIRED"
STATE_ALREADY_ROLLED_BACK = "ALREADY_ROLLED_BACK"
STATE_RESIDUAL_COPY = "RESIDUAL_COPY"
STATE_STATE_ABNORMAL = "STATE_ABNORMAL"

TERMINAL_STATES = frozenset(
    [
        STATE_DONE,
        STATE_FAILED,
        STATE_ROLLED_BACK,
        STATE_RECOVERY_REQUIRED,
        STATE_LINKED_CLEANUP_PENDING,
        STATE_ROLLBACK_FAILED,
        STATE_ALREADY_ROLLED_BACK,
        STATE_RESIDUAL_COPY,
        STATE_STATE_ABNORMAL,
    ]
)

FIELDS = (
    "job_id",
    "type",
    "tool_id",
    "source",
    "final",
    "staging",
    "tombstone",
    "restore_dir",
    "backup_dir",
    "state",
    "step",
    "intent",
    "result",
    "created_at",
    "updated_at",
    "recovered_at",
)


def _text(value):
    # type: (object) -> str
    return value if isinstance(value, str) else ""


def _mapping(value):
    # type: (object) -> Dict[str, object]
    return dict(value) if isinstance(value, dict) else {}


def validate_job_id(job_id):
    # type: (str) -> str
    value = (job_id or "").lower()
    if not JOB_ID_RE.match(value):
        raise StoreError("invalid journal job id: %r" % (job_id,))
    return value


@dataclass
class JournalEntry(object):
    """One durable job record."""

    job_id: str
    type: str = TYPE_MIGRATION
    tool_id: str = ""
    source: str = ""
    final: str = ""
    staging: str = ""
    tombstone: str = ""
    restore_dir: str = ""
    backup_dir: str = ""
    state: str = STATE_PLANNED
    step: str = ""
    intent: Dict[str, object] = field(default_factory=dict)
    result: Dict[str, object] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    recovered_at: str = ""

    @property
    def terminal(self):
        # type: () -> bool
        return self.state in TERMINAL_STATES

    def to_dict(self):
        # type: () -> Dict[str, object]
        return {
            "schema_version": SCHEMA_VERSION,
            "job_id": self.job_id,
            "type": self.type,
            "tool_id": self.tool_id,
            "source": self.source,
            "final": self.final,
            "staging": self.staging,
            "tombstone": self.tombstone,
            "restore_dir": self.restore_dir,
            "backup_dir": self.backup_dir,
            "state": self.state,
            "step": self.step,
            "intent": dict(self.intent),
            "result": dict(self.result),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "recovered_at": self.recovered_at,
        }


def entry_from_dict(payload):
    # type: (object) -> Optional[JournalEntry]
    """Validate a journal payload; returns ``None`` for an unusable record."""
    if not isinstance(payload, dict):
        return None
    job_id = _text(payload.get("job_id"))
    if not JOB_ID_RE.match(job_id):
        return None
    entry_type = _text(payload.get("type")) or TYPE_MIGRATION
    if entry_type not in TYPE_VALUES:
        return None
    state = _text(payload.get("state"))
    if not state:
        return None
    entry = JournalEntry(
        job_id=job_id,
        type=entry_type,
        tool_id=_text(payload.get("tool_id")),
        source=_text(payload.get("source")),
        final=_text(payload.get("final")),
        staging=_text(payload.get("staging")),
        tombstone=_text(payload.get("tombstone")),
        restore_dir=_text(payload.get("restore_dir")),
        backup_dir=_text(payload.get("backup_dir")),
        state=state,
        step=_text(payload.get("step")),
        intent=_mapping(payload.get("intent")),
        result=_mapping(payload.get("result")),
        created_at=_text(payload.get("created_at")),
        updated_at=_text(payload.get("updated_at")),
        recovered_at=_text(payload.get("recovered_at")),
    )
    return entry


class JournalStore(object):
    """``<config>/migration-jobs`` with atomic intent/result records."""

    def __init__(self, jobs_dir):
        # type: (str) -> None
        if not jobs_dir:
            raise StoreError("journal directory is required")
        self.jobs_dir = os.path.abspath(os.path.normpath(jobs_dir))
        self.warnings = []  # type: List[Dict[str, object]]

    def path_for(self, job_id, entry_type=TYPE_MIGRATION):
        # type: (str, str) -> str
        """Exact journal path for one job; validates the job id format."""
        value = validate_job_id(job_id)
        if entry_type not in TYPE_VALUES:
            raise StoreError("invalid journal type: %r" % (entry_type,))
        if entry_type == TYPE_ROLLBACK:
            name = value + ROLLBACK_SUFFIX
        else:
            name = value + JOURNAL_SUFFIX
        return os.path.join(self.jobs_dir, name)

    def save(self, entry, now=None):
        # type: (JournalEntry, object) -> str
        """Atomically persist one entry; stamps ``updated_at``."""
        path = self.path_for(entry.job_id, entry.type)
        stamp = utc_now(now)
        if not entry.created_at:
            entry.created_at = stamp
        entry.updated_at = stamp
        atomic_write_json(path, entry.to_dict())
        return path

    def begin(
        self,
        job_id,
        entry_type=TYPE_MIGRATION,
        tool_id="",
        source="",
        final="",
        staging="",
        tombstone="",
        restore_dir="",
        backup_dir="",
        state=STATE_PLANNED,
        step="",
        intent=None,
        now=None,
    ):
        # type: (str, str, str, str, str, str, str, str, str, str, str, object, object) -> JournalEntry
        """Write the pre-action intent record."""
        entry = JournalEntry(
            job_id=validate_job_id(job_id),
            type=entry_type,
            tool_id=tool_id or "",
            source=source or "",
            final=final or "",
            staging=staging or "",
            tombstone=tombstone or "",
            restore_dir=restore_dir or "",
            backup_dir=backup_dir or "",
            state=state or STATE_PLANNED,
            step=step or "",
            intent=_mapping(intent),
        )
        self.save(entry, now=now)
        return entry

    def load(self, job_id, entry_type=TYPE_MIGRATION):
        # type: (str, str) -> Optional[JournalEntry]
        """Read one entry; a damaged file is quarantined and returns ``None``."""
        path = self.path_for(job_id, entry_type)
        try:
            payload = read_json_file(path)
        except FileNotFoundError:
            return None
        except StoreCorruptError as exc:
            quarantined = quarantine_corrupt(path)
            self.warnings.append(
                {
                    "code": "journal_corrupt",
                    "message": str(exc),
                    "path": path,
                    "quarantined": quarantined,
                }
            )
            return None
        entry = entry_from_dict(payload)
        if entry is None or entry.job_id != job_id.lower():
            self.warnings.append(
                {
                    "code": "journal_invalid",
                    "message": "journal record is unusable",
                    "path": path,
                }
            )
            return None
        if entry.type != entry_type:
            self.warnings.append(
                {
                    "code": "journal_type_mismatch",
                    "message": "journal type is %r, expected %r"
                    % (entry.type, entry_type),
                    "path": path,
                }
            )
            return None
        return entry

    def update(self, job_id, entry_type=TYPE_MIGRATION, now=None, **changes):
        # type: (str, str, object, object) -> JournalEntry
        """Apply named field changes and persist the next state atomically."""
        entry = self.load(job_id, entry_type)
        if entry is None:
            raise StoreError("journal entry not found: %s" % job_id)
        for key, value in changes.items():
            if key not in FIELDS or key in ("job_id", "type", "created_at"):
                raise StoreError("journal field cannot be changed: %s" % key)
            if key in ("intent", "result") and not isinstance(value, dict):
                raise StoreError("%s must be a mapping" % key)
            setattr(entry, key, value)
        self.save(entry, now=now)
        return entry

    def set_intent(self, job_id, step, payload=None, state=None, entry_type=TYPE_MIGRATION, now=None):
        # type: (str, str, object, str, str, object) -> JournalEntry
        """Record what is about to happen, before the action runs."""
        entry = self.load(job_id, entry_type)
        if entry is None:
            raise StoreError("journal entry not found: %s" % job_id)
        entry.step = step or entry.step
        if state is not None:
            entry.state = state
        if payload:
            entry.intent = _mapping(payload)
        self.save(entry, now=now)
        return entry

    def set_result(self, job_id, state, payload=None, step=None, entry_type=TYPE_MIGRATION, now=None):
        # type: (str, str, object, str, str, object) -> JournalEntry
        """Record the observed result, after the action ran."""
        entry = self.load(job_id, entry_type)
        if entry is None:
            raise StoreError("journal entry not found: %s" % job_id)
        entry.state = state or entry.state
        if step is not None:
            entry.step = step
        if payload:
            entry.result = _mapping(payload)
        self.save(entry, now=now)
        return entry

    def mark_recovered(self, job_id, state, payload=None, entry_type=TYPE_MIGRATION, now=None):
        # type: (str, str, object, str, object) -> JournalEntry
        entry = self.load(job_id, entry_type)
        if entry is None:
            raise StoreError("journal entry not found: %s" % job_id)
        entry.state = state or entry.state
        entry.recovered_at = utc_now(now)
        if payload:
            entry.result = _mapping(payload)
        self.save(entry, now=now)
        return entry

    def _ignored_path(self):
        # type: () -> str
        return os.path.join(self.jobs_dir, IGNORED_FILENAME)

    def ignored(self):
        # type: () -> Dict[str, str]
        """Map ``"<type>:<job_id>"`` to the state the user hid.

        Missing or unreadable bookkeeping degrades to "nothing hidden": a
        corrupt helper file must never resurrect an error path.
        """
        path = self._ignored_path()
        try:
            payload = read_json_file(path)
        except (StoreCorruptError, StoreError, OSError):
            return {}
        entries = payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            return {}
        hidden = {}  # type: Dict[str, str]
        for item in entries:
            if not isinstance(item, dict):
                continue
            job_id = _text(item.get("job_id"))
            entry_type = _text(item.get("type")) or TYPE_MIGRATION
            state = _text(item.get("state"))
            if not JOB_ID_RE.match(job_id) or not state:
                continue
            hidden["%s:%s" % (entry_type, job_id)] = state
        return hidden

    def ignore(self, job_id, entry_type=TYPE_MIGRATION, state=""):
        # type: (str, str, str) -> None
        """Remember that the user hid this entry while it is in ``state``."""
        value = validate_job_id(job_id)
        if entry_type not in TYPE_VALUES:
            raise StoreError("invalid journal type: %r" % (entry_type,))
        hidden = self.ignored()
        key = "%s:%s" % (entry_type, value)
        hidden[key] = state or ""
        now = utc_now()
        entries = []
        for name in sorted(hidden):
            kind, job = name.split(":", 1)
            entries.append(
                {
                    "job_id": job,
                    "type": kind,
                    "state": hidden[name],
                    "at": now,
                }
            )
        atomic_write_json(self._ignored_path(), {"entries": entries})

    def list_pending(self):
        # type: () -> List[JournalEntry]
        """Return non-terminal records, newest first."""
        self.warnings = []
        entries = []  # type: List[JournalEntry]
        try:
            with os.scandir(self.jobs_dir) as scan:
                names = sorted(entry.name for entry in scan)
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise StoreError("cannot list journal directory: %s" % exc)

        for name in names:
            if not name.endswith(JOURNAL_SUFFIX):
                continue
            if ".tmp-" in name or ".corrupt-" in name:
                continue
            if name.endswith(ROLLBACK_SUFFIX):
                job_part = name[: -len(ROLLBACK_SUFFIX)]
                entry_type = TYPE_ROLLBACK
            else:
                job_part = name[: -len(JOURNAL_SUFFIX)]
                entry_type = TYPE_MIGRATION
            if not JOB_ID_RE.match(job_part):
                continue
            entry = self.load(job_part, entry_type)
            if entry is None or entry.terminal:
                continue
            entries.append(entry)
        entries.sort(key=lambda item: item.updated_at, reverse=True)
        return entries

    def list_all(self):
        # type: () -> List[JournalEntry]
        """Return every readable record; used by diagnostics."""
        pending = self.list_pending()
        terminal = []  # type: List[JournalEntry]
        try:
            with os.scandir(self.jobs_dir) as scan:
                names = sorted(entry.name for entry in scan)
        except FileNotFoundError:
            return pending
        for name in names:
            if not name.endswith(JOURNAL_SUFFIX):
                continue
            if ".tmp-" in name or ".corrupt-" in name:
                continue
            if name.endswith(ROLLBACK_SUFFIX):
                job_part = name[: -len(ROLLBACK_SUFFIX)]
                entry_type = TYPE_ROLLBACK
            else:
                job_part = name[: -len(JOURNAL_SUFFIX)]
                entry_type = TYPE_MIGRATION
            if not JOB_ID_RE.match(job_part):
                continue
            entry = self.load(job_part, entry_type)
            if entry is not None and entry.terminal:
                terminal.append(entry)
        combined = pending + terminal
        combined.sort(key=lambda item: item.updated_at, reverse=True)
        return combined

    def remove(self, job_id, entry_type=TYPE_MIGRATION):
        # type: (str, str) -> bool
        """Delete one exact journal file; returns False when absent."""
        path = self.path_for(job_id, entry_type)
        try:
            os.unlink(path)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise StoreError("cannot remove journal %s: %s" % (path, exc))
        return True


__all__ = [
    "FIELDS",
    "JOB_ID_RE",
    "JOURNAL_SUFFIX",
    "JournalEntry",
    "JournalStore",
    "ROLLBACK_SUFFIX",
    "SCHEMA_VERSION",
    "STATE_ALREADY_ROLLED_BACK",
    "STATE_BACKED_UP",
    "STATE_COPIED",
    "STATE_COPYING",
    "STATE_COPY_VERIFIED",
    "STATE_DONE",
    "STATE_FAILED",
    "STATE_LINKED",
    "STATE_LINKED_CLEANUP_PENDING",
    "STATE_PLANNED",
    "STATE_RECOVERY_REQUIRED",
    "STATE_RESIDUAL_COPY",
    "STATE_ROLLBACK_COPIED",
    "STATE_ROLLBACK_CONFIG_RESTORED",
    "STATE_ROLLBACK_COPYING",
    "STATE_ROLLBACK_FAILED",
    "STATE_ROLLBACK_LINK_REMOVED",
    "STATE_ROLLBACK_PLANNED",
    "STATE_ROLLBACK_RECOVERY_REQUIRED",
    "STATE_ROLLBACK_SOURCE_READY",
    "STATE_ROLLBACK_VERIFIED",
    "STATE_ROLLED_BACK",
    "STATE_SOURCE_RENAMED",
    "STATE_STATE_ABNORMAL",
    "STATE_TARGET_READY",
    "STATE_VERIFIED_LINK",
    "TERMINAL_STATES",
    "TYPE_MIGRATION",
    "TYPE_ROLLBACK",
    "TYPE_VALUES",
    "entry_from_dict",
    "validate_job_id",
]
