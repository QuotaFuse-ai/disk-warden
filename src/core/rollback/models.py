# -*- coding: utf-8 -*-
"""Models for one-click rollback (architecture 6.7 / AC-ROL-01..08).

Rollback is the inverse of a completed migration: the junction is removed,
the target data is copied back through a same-volume restore slot, verified,
renamed into place, configuration is restored from the migration manifest and
only then is the target copy deleted.  These objects describe the facts the
engine observed and the outcome it recorded; they never perform I/O.

Python 3.8 compatible (decision 0005).
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..store import journal as journal_states
from ..store.journal import (
    STATE_ALREADY_ROLLED_BACK,
    STATE_RESIDUAL_COPY,
    STATE_ROLLBACK_COPIED,
    STATE_ROLLBACK_CONFIG_RESTORED,
    STATE_ROLLBACK_COPYING,
    STATE_ROLLBACK_FAILED,
    STATE_ROLLBACK_LINK_REMOVED,
    STATE_ROLLBACK_PLANNED,
    STATE_ROLLBACK_RECOVERY_REQUIRED,
    STATE_ROLLBACK_SOURCE_READY,
    STATE_ROLLBACK_VERIFIED,
    STATE_ROLLED_BACK,
    STATE_STATE_ABNORMAL,
)

STAGE_PRECHECK = "precheck"
STAGE_LINK_REMOVE = "link_remove"
STAGE_COPY = "copy"
STAGE_VERIFY = "verify"
STAGE_RESTORE = "restore"
STAGE_CONFIG = "config"
STAGE_CLEANUP = "cleanup"
STAGE_RECORD = "record"
STAGE_RECOVER = "recover"

STAGES = (
    STAGE_PRECHECK,
    STAGE_LINK_REMOVE,
    STAGE_COPY,
    STAGE_VERIFY,
    STAGE_RESTORE,
    STAGE_CONFIG,
    STAGE_CLEANUP,
    STAGE_RECORD,
    STAGE_RECOVER,
)

MODE_ROLLBACK = "rollback"
MODE_ALREADY_ROLLED_BACK = "already_rolled_back"
MODE_RESIDUAL_COPY = "residual_copy"
MODE_BLOCKED = "blocked"
MODES = (
    MODE_ROLLBACK,
    MODE_ALREADY_ROLLED_BACK,
    MODE_RESIDUAL_COPY,
    MODE_BLOCKED,
)

SOURCE_KIND_MISSING = "missing"
SOURCE_KIND_JUNCTION = "junction"
SOURCE_KIND_LINK = "link"
SOURCE_KIND_DIR = "dir"
SOURCE_KIND_FILE = "file"
SOURCE_KIND_OTHER = "other"

CONFIG_KEEP = "keep"
CONFIG_RESTORE = "restore"
CONFIG_CHOICES = (CONFIG_KEEP, CONFIG_RESTORE)

CONFIG_STATUS_NOT_REQUIRED = "not_required"
CONFIG_STATUS_RESTORED = "restored"
CONFIG_STATUS_KEPT = "kept"
CONFIG_STATUS_UNAVAILABLE = "unavailable"
CONFIG_STATUS_PARTIAL = "partial"
CONFIG_STATUS_FAILED = "failed"

CONFIG_ACTION_IDENTICAL = "identical"
CONFIG_ACTION_RESTORED_MISSING = "restored_missing"
CONFIG_ACTION_KEPT_USER = "kept_user"
CONFIG_ACTION_RESTORED_USER_CHOICE = "restored_user_choice"
CONFIG_ACTION_BACKUP_MISSING = "backup_missing"
CONFIG_ACTION_BACKUP_CORRUPT = "backup_corrupt"
CONFIG_ACTION_RESTORE_FAILED = "restore_failed"

ERROR_INVALID_INPUT = "INVALID_ROLLBACK_INPUT"
ERROR_PRECHECK = "PRECHECK_FAILED"
ERROR_TARGET_UNAVAILABLE = "TARGET_UNAVAILABLE"
ERROR_PROCESS_BUSY = "PROCESS_BUSY"
ERROR_INSUFFICIENT_SPACE = "INSUFFICIENT_SPACE"
ERROR_LINK_REMOVE = "LINK_REMOVE_FAILED"
ERROR_COPY = "ROLLBACK_COPY_FAILED"
ERROR_VERIFY = "ROLLBACK_VERIFY_FAILED"
ERROR_RESTORE = "SOURCE_RESTORE_FAILED"
ERROR_STATE = "STATE_ABNORMAL"
ERROR_RECOVERY = "RECOVERY_REQUIRED"
ERROR_CANCELLED = "CANCELLED"
ERROR_TARGET_DELETE = "TARGET_DELETE_FAILED"
ERROR_CONFIG = "CONFIG_RESTORE_FAILED"

TERMINAL_STATES = frozenset(
    [
        STATE_ROLLED_BACK,
        STATE_ALREADY_ROLLED_BACK,
        STATE_RESIDUAL_COPY,
        STATE_ROLLBACK_FAILED,
        STATE_STATE_ABNORMAL,
    ]
)


@dataclass(frozen=True)
class RollbackRequest:
    """One migrated item to roll back.

    ``job_id`` identifies the original migration; its journal record supplies
    the recorded source/target/backup paths.  ``process_names`` are checked
    before any mutation: a running or unknown process blocks the rollback
    (architecture 6.1).  ``config_choice`` decides scenario C (the user edited
    a backed-up config): ``keep`` is the default and never overwrites.
    """

    job_id: str
    tool_id: str
    source_path: str
    final_path: str
    backup_dir: Optional[str] = None
    process_names: Tuple[str, ...] = ()
    expected_bytes: Optional[int] = None
    expected_files: Optional[int] = None
    confirm_residual_delete: bool = False
    config_choice: str = CONFIG_KEEP
    log_path: Optional[str] = None


@dataclass
class RollbackPrecheck:
    """Everything observed before the first mutation (AC-ROL-02/05/08)."""

    ok: bool
    mode: str = MODE_BLOCKED
    error_code: Optional[str] = None
    error_message: str = ""
    source_exists: bool = False
    source_kind: str = SOURCE_KIND_MISSING
    source_link_target: str = ""
    source_link_matches: bool = False
    target_exists: bool = False
    target_owned: bool = False
    target_bytes: int = 0
    target_files: int = 0
    free_bytes: Optional[int] = None
    required_bytes: int = 0
    free_space_ok: bool = True
    process: Optional[Dict[str, object]] = None
    backup_dir: str = ""
    warnings: List[str] = field(default_factory=list)

    @property
    def can_proceed(self):
        # type: () -> bool
        return self.ok and self.mode == MODE_ROLLBACK

    def to_dict(self):
        # type: () -> Dict[str, object]
        return {
            "ok": self.ok,
            "mode": self.mode,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "source_exists": self.source_exists,
            "source_kind": self.source_kind,
            "source_link_target": self.source_link_target,
            "source_link_matches": self.source_link_matches,
            "target_exists": self.target_exists,
            "target_owned": self.target_owned,
            "target_bytes": self.target_bytes,
            "target_files": self.target_files,
            "free_bytes": self.free_bytes,
            "required_bytes": self.required_bytes,
            "free_space_ok": self.free_space_ok,
            "process": dict(self.process) if self.process is not None else None,
            "backup_dir": self.backup_dir,
            "warnings": list(self.warnings),
        }


@dataclass
class ConfigRestoreOutcome:
    """AC-ROL-06 scenarios A/B/C/D as observed per config file."""

    status: str = CONFIG_STATUS_NOT_REQUIRED
    complete: bool = True
    restored: List[str] = field(default_factory=list)
    kept: List[str] = field(default_factory=list)
    unavailable: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    details: List[Dict[str, object]] = field(default_factory=list)
    reason: str = ""

    @property
    def ok(self):
        # type: () -> bool
        return self.complete and not self.failed

    def to_dict(self):
        # type: () -> Dict[str, object]
        return {
            "status": self.status,
            "ok": self.ok,
            "complete": self.complete,
            "restored": list(self.restored),
            "kept": list(self.kept),
            "unavailable": list(self.unavailable),
            "failed": list(self.failed),
            "details": [dict(item) for item in self.details],
            "reason": self.reason,
        }


@dataclass
class RollbackResult:
    """Outcome of one rollback attempt.

    ``ok`` is True only for ``ROLLED_BACK`` and ``ALREADY_ROLLED_BACK``.
    ``data_kept`` means at least one complete copy still exists; it is True
    on every failure path (architecture 6.1/6.7).
    """

    job_id: str
    tool_id: str
    source_path: str
    final_path: str
    state: str = STATE_ROLLBACK_FAILED
    restore_dir: str = ""
    backup_dir: str = ""
    error_code: Optional[str] = None
    error_message: str = ""
    blocked: bool = False
    link_removed: bool = False
    source_restored: bool = False
    target_removed: bool = False
    residual_copy: bool = False
    data_kept: bool = False
    relinked: bool = False
    process: Optional[Dict[str, object]] = None
    precheck: Optional[RollbackPrecheck] = None
    copy: Optional[object] = None
    verify: Optional[object] = None
    config: Optional[ConfigRestoreOutcome] = None
    steps: List[Dict[str, object]] = field(default_factory=list)
    diagnostics: List[str] = field(default_factory=list)
    manual_steps: List[str] = field(default_factory=list)
    history_recorded: bool = False
    learned_updated: bool = False
    recovered: bool = False
    warnings: List[str] = field(default_factory=list)

    @property
    def ok(self):
        # type: () -> bool
        return self.state in (STATE_ROLLED_BACK, STATE_ALREADY_ROLLED_BACK)

    def to_dict(self):
        # type: () -> Dict[str, object]
        copy_result = self.copy
        verify_result = self.verify
        return {
            "job_id": self.job_id,
            "tool_id": self.tool_id,
            "source_path": self.source_path,
            "final_path": self.final_path,
            "state": self.state,
            "ok": self.ok,
            "restore_dir": self.restore_dir,
            "backup_dir": self.backup_dir,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "blocked": self.blocked,
            "link_removed": self.link_removed,
            "source_restored": self.source_restored,
            "target_removed": self.target_removed,
            "residual_copy": self.residual_copy,
            "data_kept": self.data_kept,
            "relinked": self.relinked,
            "process": dict(self.process) if self.process is not None else None,
            "precheck": self.precheck.to_dict() if self.precheck else None,
            "copy": copy_result.to_dict()
            if hasattr(copy_result, "to_dict")
            else copy_result,
            "verify": verify_result.to_dict()
            if hasattr(verify_result, "to_dict")
            else verify_result,
            "config": self.config.to_dict() if self.config else None,
            "steps": [dict(step) for step in self.steps],
            "diagnostics": list(self.diagnostics),
            "manual_steps": list(self.manual_steps),
            "history_recorded": self.history_recorded,
            "learned_updated": self.learned_updated,
            "recovered": self.recovered,
            "warnings": list(self.warnings),
        }


__all__ = [
    "CONFIG_ACTION_BACKUP_CORRUPT",
    "CONFIG_ACTION_BACKUP_MISSING",
    "CONFIG_ACTION_IDENTICAL",
    "CONFIG_ACTION_KEPT_USER",
    "CONFIG_ACTION_RESTORED_MISSING",
    "CONFIG_ACTION_RESTORED_USER_CHOICE",
    "CONFIG_ACTION_RESTORE_FAILED",
    "CONFIG_CHOICES",
    "CONFIG_KEEP",
    "CONFIG_RESTORE",
    "CONFIG_STATUS_FAILED",
    "CONFIG_STATUS_KEPT",
    "CONFIG_STATUS_NOT_REQUIRED",
    "CONFIG_STATUS_PARTIAL",
    "CONFIG_STATUS_RESTORED",
    "CONFIG_STATUS_UNAVAILABLE",
    "ConfigRestoreOutcome",
    "ERROR_CANCELLED",
    "ERROR_CONFIG",
    "ERROR_COPY",
    "ERROR_INSUFFICIENT_SPACE",
    "ERROR_INVALID_INPUT",
    "ERROR_LINK_REMOVE",
    "ERROR_PRECHECK",
    "ERROR_PROCESS_BUSY",
    "ERROR_RECOVERY",
    "ERROR_RESTORE",
    "ERROR_STATE",
    "ERROR_TARGET_DELETE",
    "ERROR_TARGET_UNAVAILABLE",
    "ERROR_VERIFY",
    "MODE_ALREADY_ROLLED_BACK",
    "MODE_BLOCKED",
    "MODE_RESIDUAL_COPY",
    "MODE_ROLLBACK",
    "MODES",
    "RollbackPrecheck",
    "RollbackRequest",
    "RollbackResult",
    "SOURCE_KIND_DIR",
    "SOURCE_KIND_FILE",
    "SOURCE_KIND_JUNCTION",
    "SOURCE_KIND_LINK",
    "SOURCE_KIND_MISSING",
    "SOURCE_KIND_OTHER",
    "STAGES",
    "STAGE_CLEANUP",
    "STAGE_CONFIG",
    "STAGE_COPY",
    "STAGE_LINK_REMOVE",
    "STAGE_PRECHECK",
    "STAGE_RECORD",
    "STAGE_RECOVER",
    "STAGE_RESTORE",
    "STAGE_VERIFY",
    "STATE_ALREADY_ROLLED_BACK",
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
    "STATE_STATE_ABNORMAL",
    "TERMINAL_STATES",
    "journal_states",
]
