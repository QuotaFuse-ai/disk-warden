# -*- coding: utf-8 -*-
"""Migration engines (T09 copy half, T10 swap/link/recovery half).

The frozen architecture places this package at ``src/core/migrate`` while the
task briefs write ``src/migrate``; the architecture layout wins (same
deviation as T06-T08, documented in docs/tasks/TASK-009/).

Engine A (T09) never renames or deletes source data:
    prepare_copy(request, ...) -> MigrationResult

Engine B (T10) consumes a successful ``MigrationResult`` and runs S5-S9:
    complete_migration(result, ...) -> CompletionResult

Crash recovery (T10) observes the filesystem and the recorded journal state:
    recover_job(job_id, ..., recorded_state=...) -> CompletionResult

Supporting helpers:
    verify_trees, backup_config, discover_config_files
    MigrationOps (injectable platform operations for fault drills)
    ensure_target_ready / ensure_source_renamed / restore_source
    ensure_link / verify_link / remove_own_link
    remove_tombstone / remove_owned_tree / remove_tree
"""

from .backup import (
    BackupError,
    CONFIG_FILENAMES,
    backup_config,
    discover_config_files,
)
from .cleanup import (
    UnsafeCleanupError,
    remove_owned_tree,
    remove_tombstone,
    remove_tree,
)
from .copy import copy_to_staging, sample_tree
from .engine import prepare_copy
from .marker import (
    SCHEMA_VERSION as OWNER_MARKER_SCHEMA_VERSION,
    marker_path,
    owns_directory,
    read_owner_marker,
    write_owner_marker,
)
from .models import (
    BackupEntry,
    BackupManifest,
    CompletionResult,
    CopyResult,
    ERROR_BACKUP,
    ERROR_CANCELLED,
    ERROR_CLEANUP,
    ERROR_COPY,
    ERROR_INPUT,
    ERROR_LINK,
    ERROR_LINK_VERIFY,
    ERROR_PREPARE,
    ERROR_PROCESS_BUSY,
    ERROR_RECOVERY,
    ERROR_SWAP,
    ERROR_VERIFY,
    MigrationCancelled,
    MigrationRequest,
    MigrationResult,
    RECORDED_STATES,
    STAGES,
    STAGES_COMPLETE,
    STAGES_COPY,
    STAGE_BACKUP,
    STAGE_CLEANUP,
    STAGE_COPY,
    STAGE_LINK,
    STAGE_LINK_VERIFY,
    STAGE_PREPARE,
    STAGE_RECOVER,
    STAGE_SWAP,
    STAGE_VERIFY,
    STATE_BACKED_UP,
    STATE_COPIED,
    STATE_COPYING,
    STATE_COPY_VERIFIED,
    STATE_DONE,
    STATE_FAILED,
    STATE_LINKED,
    STATE_LINKED_CLEANUP_PENDING,
    STATE_PLANNED,
    STATE_RECOVERY_REQUIRED,
    STATE_ROLLED_BACK,
    STATE_SOURCE_RENAMED,
    STATE_TARGET_READY,
    STATE_VERIFIED_LINK,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PROGRESS,
    STATUS_SKIPPED,
    STATUS_START,
    SizeMismatch,
    TERMINAL_STATES,
    VerifyResult,
)
from .ops import MigrationOps
from .paths import (
    PathValidationError,
    job_prefix,
    new_job_id,
    staging_parent,
    staging_path,
)
from .progress import Emitter
from .recover import Observation, observe, recover_job
from .swap import (
    SwapError,
    discard_staging,
    discard_target,
    ensure_source_renamed,
    ensure_target_ready,
    restore_source,
)
from .verify import verify_trees

# ``link`` and ``complete`` are imported last: their modules import the
# siblings above via ``from . import ...`` and depend on them being loaded.
from .link import (
    LinkCheck,
    LinkStepError,
    ensure_link,
    find_probe,
    remove_own_link,
    verify_link,
)
from .complete import complete_migration

__all__ = [
    "BackupEntry",
    "BackupError",
    "BackupManifest",
    "CONFIG_FILENAMES",
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
    "Emitter",
    "LinkCheck",
    "LinkStepError",
    "MigrationCancelled",
    "MigrationOps",
    "MigrationRequest",
    "MigrationResult",
    "OWNER_MARKER_SCHEMA_VERSION",
    "Observation",
    "PathValidationError",
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
    "SwapError",
    "TERMINAL_STATES",
    "UnsafeCleanupError",
    "VerifyResult",
    "backup_config",
    "complete_migration",
    "copy_to_staging",
    "discard_staging",
    "discard_target",
    "discover_config_files",
    "ensure_link",
    "ensure_source_renamed",
    "ensure_target_ready",
    "find_probe",
    "job_prefix",
    "marker_path",
    "new_job_id",
    "observe",
    "owns_directory",
    "prepare_copy",
    "read_owner_marker",
    "recover_job",
    "remove_owned_tree",
    "remove_own_link",
    "remove_tombstone",
    "remove_tree",
    "restore_source",
    "sample_tree",
    "staging_parent",
    "staging_path",
    "verify_link",
    "verify_trees",
    "write_owner_marker",
]
