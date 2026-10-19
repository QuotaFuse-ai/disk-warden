# -*- coding: utf-8 -*-
"""Models for the migration engine (T09 copy half, T10 swap/link half).

These objects describe facts only: what was backed up, what robocopy did,
whether source and staging agree, and how the source was replaced by a
junction.  Task 10 never deletes a source tree recursively: it renames the
source to a tombstone and removes that tombstone only after the link has been
verified (architecture 6.1).

Python 3.8 compatible (decision 0005).
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

STAGE_PREPARE = "prepare"
STAGE_BACKUP = "backup"
STAGE_COPY = "copy"
STAGE_VERIFY = "verify"
STAGE_SWAP = "swap"
STAGE_LINK = "link"
STAGE_LINK_VERIFY = "link_verify"
STAGE_CLEANUP = "cleanup"
STAGE_RECOVER = "recover"

STAGES_COPY = (STAGE_PREPARE, STAGE_BACKUP, STAGE_COPY, STAGE_VERIFY)
STAGES_COMPLETE = (
    STAGE_SWAP,
    STAGE_LINK,
    STAGE_LINK_VERIFY,
    STAGE_CLEANUP,
    STAGE_RECOVER,
)
STAGES = STAGES_COPY + STAGES_COMPLETE

STATUS_START = "start"
STATUS_PROGRESS = "progress"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

ERROR_PREPARE = "PREPARE_FAILED"
ERROR_BACKUP = "BACKUP_FAILED"
ERROR_COPY = "COPY_FAILED"
ERROR_VERIFY = "VERIFY_FAILED"
ERROR_CANCELLED = "CANCELLED"
ERROR_INPUT = "INVALID_RESULT"
ERROR_SWAP = "SWAP_FAILED"
ERROR_PROCESS_BUSY = "PROCESS_BUSY"
ERROR_LINK = "LINK_FAILED"
ERROR_LINK_VERIFY = "LINK_VERIFY_FAILED"
ERROR_CLEANUP = "CLEANUP_FAILED"
ERROR_RECOVERY = "RECOVERY_REQUIRED"

# Journal states (architecture 6.4).  T10 reports the terminal subset and
# accepts the whole set as the recorded state passed to recovery.
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

RECORDED_STATES = (
    STATE_PLANNED,
    STATE_BACKED_UP,
    STATE_COPYING,
    STATE_COPIED,
    STATE_COPY_VERIFIED,
    STATE_TARGET_READY,
    STATE_SOURCE_RENAMED,
    STATE_LINKED,
    STATE_VERIFIED_LINK,
    STATE_LINKED_CLEANUP_PENDING,
    STATE_DONE,
    STATE_FAILED,
    STATE_ROLLED_BACK,
    STATE_RECOVERY_REQUIRED,
)

TERMINAL_STATES = (
    STATE_DONE,
    STATE_FAILED,
    STATE_ROLLED_BACK,
    STATE_RECOVERY_REQUIRED,
    STATE_LINKED_CLEANUP_PENDING,
)


class MigrationCancelled(RuntimeError):
    """Raised when a cancel token fires mid-migration.

    Cancellation is not a failure result: the partial staging copy is removed
    and callers should treat the job as abandoned with the source untouched.
    """


@dataclass(frozen=True)
class MigrationRequest:
    """One source path to stage-copy into the target root.

    ``target_root`` is the Disk Warden data root (``D:\\DiskWarden\\data`` by
    default); the final destination is ``<target_root>/<tool_id>/<basename>``.
    ``config_root`` is the tool directory scanned for config files (defaults
    to ``source_path``, which is usually a cache directory).  ``config_files``
    are explicit config files (absolute, or relative to ``config_root``) to
    back up before the copy; when empty, well-known config names are
    discovered below ``config_root``.
    """

    tool_id: str
    source_path: str
    target_root: str
    backup_root: Optional[str] = None
    config_root: Optional[str] = None
    config_files: Tuple[str, ...] = ()
    expected_bytes: Optional[int] = None
    expected_files: Optional[int] = None
    job_id: Optional[str] = None


@dataclass
class BackupEntry:
    """One backed-up config file and its V3 checksum."""

    rel_path: str
    source_path: str
    backup_path: str
    size: int
    sha256: str

    def to_dict(self):
        # type: () -> Dict[str, object]
        return {
            "rel_path": self.rel_path,
            "source_path": self.source_path,
            "backup_path": self.backup_path,
            "size": self.size,
            "sha256": self.sha256,
        }


@dataclass
class BackupManifest:
    """V3 manifest written last, so its presence means the backup finished."""

    schema_version: int
    job_id: str
    tool_id: str
    source_root: str
    backup_dir: str
    created_at: str
    entries: List[BackupEntry] = field(default_factory=list)
    skipped: List[Dict[str, str]] = field(default_factory=list)

    @property
    def total_bytes(self):
        # type: () -> int
        return sum(entry.size for entry in self.entries)

    @property
    def file_count(self):
        # type: () -> int
        return len(self.entries)

    def to_dict(self):
        # type: () -> Dict[str, object]
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "tool_id": self.tool_id,
            "source_root": self.source_root,
            "backup_dir": self.backup_dir,
            "created_at": self.created_at,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "entries": [entry.to_dict() for entry in self.entries],
            "skipped": [dict(item) for item in self.skipped],
        }


@dataclass
class CopyResult:
    """Outcome of the robocopy run that produced the staging copy."""

    exit_code: int
    staging_path: str
    log_path: str
    duration_seconds: float
    sampled_bytes: int = 0
    sampled_files: int = 0

    @property
    def ok(self):
        # type: () -> bool
        return 0 <= self.exit_code < 8

    def to_dict(self):
        # type: () -> Dict[str, object]
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "staging_path": self.staging_path,
            "log_path": self.log_path,
            "duration_seconds": self.duration_seconds,
            "sampled_bytes": self.sampled_bytes,
            "sampled_files": self.sampled_files,
        }


@dataclass
class SizeMismatch:
    """A relative path whose byte size differs between source and staging."""

    rel_path: str
    source_bytes: int
    target_bytes: int

    def to_dict(self):
        # type: () -> Dict[str, object]
        return {
            "rel_path": self.rel_path,
            "source_bytes": self.source_bytes,
            "target_bytes": self.target_bytes,
        }


@dataclass
class VerifyResult:
    """V2 verification: path set, per-file bytes, file count and total bytes."""

    ok: bool
    reason: str = ""
    source_file_count: int = 0
    target_file_count: int = 0
    source_bytes: int = 0
    target_bytes: int = 0
    missing: List[str] = field(default_factory=list)
    extra: List[str] = field(default_factory=list)
    size_mismatches: List[SizeMismatch] = field(default_factory=list)
    missing_count: int = 0
    extra_count: int = 0
    size_mismatch_count: int = 0
    errors: List[str] = field(default_factory=list)
    reparse_paths: List[str] = field(default_factory=list)

    def to_dict(self):
        # type: () -> Dict[str, object]
        return {
            "ok": self.ok,
            "reason": self.reason,
            "source_file_count": self.source_file_count,
            "target_file_count": self.target_file_count,
            "source_bytes": self.source_bytes,
            "target_bytes": self.target_bytes,
            "missing": list(self.missing),
            "extra": list(self.extra),
            "size_mismatches": [
                mismatch.to_dict() for mismatch in self.size_mismatches
            ],
            "missing_count": self.missing_count,
            "extra_count": self.extra_count,
            "size_mismatch_count": self.size_mismatch_count,
            "errors": list(self.errors),
            "reparse_paths": list(self.reparse_paths),
        }


@dataclass
class MigrationResult:
    """Everything callers need to decide the next step (T10 swap/link).

    ``staging_path`` is a complete, verified copy; the source has not been
    touched.  ``final_path`` is where T10 will rename the staging directory.
    """

    job_id: str
    tool_id: str
    source_path: str
    final_path: str
    staging_path: str
    ok: bool
    error_code: Optional[str] = None
    error_message: str = ""
    cancelled: bool = False
    temp_removed: bool = False
    backup_dir: Optional[str] = None
    backup: Optional[BackupManifest] = None
    copy: Optional[CopyResult] = None
    verify: Optional[VerifyResult] = None

    def to_dict(self):
        # type: () -> Dict[str, object]
        data = {
            "job_id": self.job_id,
            "tool_id": self.tool_id,
            "source_path": self.source_path,
            "final_path": self.final_path,
            "staging_path": self.staging_path,
            "ok": self.ok,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "cancelled": self.cancelled,
            "temp_removed": self.temp_removed,
            "backup_dir": self.backup_dir,
            "backup": self.backup.to_dict() if self.backup else None,
            "copy": self.copy.to_dict() if self.copy else None,
            "verify": self.verify.to_dict() if self.verify else None,
        }
        return data


@dataclass
class CompletionResult:
    """Outcome of the second half (S5-S9) and of recovery.

    ``state`` is the architecture 6.4 state name.  ``ok`` is True for ``DONE``
    and ``LINKED_CLEANUP_PENDING``: the link is verified and only duplicate
    tombstone data may remain.
    """

    job_id: str
    tool_id: str
    source_path: str
    final_path: str
    staging_path: str
    tombstone_path: str
    state: str = STATE_FAILED
    error_code: Optional[str] = None
    error_message: str = ""
    rolled_back: bool = False
    link_created: bool = False
    link_target: Optional[str] = None
    link_verified: bool = False
    source_removed: bool = False
    cleanup_errors: List[str] = field(default_factory=list)
    process: Optional[Dict[str, object]] = None
    verify: Optional[VerifyResult] = None
    steps: List[Dict[str, object]] = field(default_factory=list)

    @property
    def ok(self):
        # type: () -> bool
        return self.state in (STATE_DONE, STATE_LINKED_CLEANUP_PENDING)

    @property
    def cleanup_pending(self):
        # type: () -> bool
        return self.state == STATE_LINKED_CLEANUP_PENDING

    def to_dict(self):
        # type: () -> Dict[str, object]
        return {
            "job_id": self.job_id,
            "tool_id": self.tool_id,
            "source_path": self.source_path,
            "final_path": self.final_path,
            "staging_path": self.staging_path,
            "tombstone_path": self.tombstone_path,
            "state": self.state,
            "ok": self.ok,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "rolled_back": self.rolled_back,
            "link_created": self.link_created,
            "link_target": self.link_target,
            "link_verified": self.link_verified,
            "source_removed": self.source_removed,
            "cleanup_pending": self.cleanup_pending,
            "cleanup_errors": list(self.cleanup_errors),
            "process": dict(self.process) if self.process is not None else None,
            "verify": self.verify.to_dict() if self.verify is not None else None,
            "steps": [dict(step) for step in self.steps],
        }


__all__ = [
    "BackupEntry",
    "BackupManifest",
    "CompletionResult",
    "CopyResult",
    "ERROR_BACKUP",
    "ERROR_CANCELLED",
    "ERROR_CLEANUP",
    "ERROR_COPY",
    "ERROR_INPUT",
    "ERROR_LINK",
    "ERROR_LINK_VERIFY",
    "ERROR_PREPARE",
    "ERROR_PROCESS_BUSY",
    "ERROR_RECOVERY",
    "ERROR_SWAP",
    "ERROR_VERIFY",
    "MigrationRequest",
    "MigrationResult",
    "MigrationCancelled",
    "RECORDED_STATES",
    "STAGES",
    "STAGES_COMPLETE",
    "STAGES_COPY",
    "STAGE_BACKUP",
    "STAGE_CLEANUP",
    "STAGE_COPY",
    "STAGE_LINK",
    "STAGE_LINK_VERIFY",
    "STAGE_PREPARE",
    "STAGE_RECOVER",
    "STAGE_SWAP",
    "STAGE_VERIFY",
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
    "STATE_ROLLED_BACK",
    "STATE_SOURCE_RENAMED",
    "STATE_TARGET_READY",
    "STATE_VERIFIED_LINK",
    "STATUS_DONE",
    "STATUS_FAILED",
    "STATUS_PROGRESS",
    "STATUS_SKIPPED",
    "STATUS_START",
    "SizeMismatch",
    "TERMINAL_STATES",
    "VerifyResult",
]
