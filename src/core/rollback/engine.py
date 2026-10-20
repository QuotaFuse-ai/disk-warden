# -*- coding: utf-8 -*-
"""One-click rollback engine (architecture 6.7 / AC-ROL-01..08).

The flow is the inverse of ``complete_migration``:

    PRECHECK -> remove junction -> copy target back -> verify
             -> rename restore slot into place -> restore config
             -> delete the recorded target -> history + learned

Every mutation is preceded by a journal intent and followed by a journal
result, so a crash leaves enough evidence for ``rollback.recovery``.  Nothing
is deleted before the restored source has been verified, and any deletion is
limited to the exact target path recorded for this job and proven to carry
this job's ownership marker (architecture 6.1/6.5).

Failures keep at least one complete copy.  When the junction was already
removed the engine tries to re-link the source to the intact target before
reporting ``ROLLBACK_FAILED``; if that also fails the record becomes
``ROLLBACK_RECOVERY_REQUIRED`` and no further automatic deletion happens.

Python 3.8 compatible (decision 0005).
"""

import os
import shutil
from typing import Dict, List, Optional, Tuple

from ...platform import link_target_matches
from ..migrate import cleanup, marker, paths
from ..migrate import link as link_step
from ..migrate.copy import DEFAULT_SAMPLE_SECONDS, LOG_NAME, copy_to_staging
from ..migrate.models import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_START,
    MigrationCancelled,
)
from ..migrate.ops import MigrationOps
from ..migrate.progress import Emitter
from ..migrate.verify import verify_trees
from ..rules import item_id_for
from ..scan.cancel import is_cancelled
from ..scan.process import detect_processes
from ..store.atomic import StoreError
from ..store.history import RESULT_ROLLED_BACK, HistoryStore
from ..store.journal import (
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
    TYPE_MIGRATION,
    TYPE_ROLLBACK,
    JournalStore,
)
from ..store.learned import LearnedRulesStore
from .config import restore_configs
from .models import (
    CONFIG_CHOICES,
    ERROR_CANCELLED,
    ERROR_COPY,
    ERROR_INSUFFICIENT_SPACE,
    ERROR_INVALID_INPUT,
    ERROR_LINK_REMOVE,
    ERROR_PROCESS_BUSY,
    ERROR_RECOVERY,
    ERROR_RESTORE,
    ERROR_STATE,
    ERROR_TARGET_DELETE,
    ERROR_TARGET_UNAVAILABLE,
    ERROR_VERIFY,
    MODE_ALREADY_ROLLED_BACK,
    MODE_BLOCKED,
    MODE_RESIDUAL_COPY,
    MODE_ROLLBACK,
    SOURCE_KIND_DIR,
    SOURCE_KIND_FILE,
    SOURCE_KIND_JUNCTION,
    SOURCE_KIND_LINK,
    SOURCE_KIND_MISSING,
    SOURCE_KIND_OTHER,
    STAGE_CLEANUP,
    STAGE_CONFIG,
    STAGE_COPY,
    STAGE_LINK_REMOVE,
    STAGE_PRECHECK,
    STAGE_RECORD,
    STAGE_RESTORE,
    STAGE_VERIFY,
    STATE_ALREADY_ROLLED_BACK,
    STATE_RESIDUAL_COPY,
    STATE_STATE_ABNORMAL,
    RollbackPrecheck,
    RollbackRequest,
    RollbackResult,
)

ROLLBACK_RULE_ID = "rollback"
MAX_DETAIL_ITEMS = 20


class _ToolStub(object):
    """Minimal catalog-tool shape accepted by ``detect_processes``."""

    def __init__(self, tool_id, process_names):
        # type: (str, Tuple[str, ...]) -> None
        self.id = tool_id
        self.processes = tuple(process_names or ())


def _normalise_request(request):
    # type: (RollbackRequest) -> Tuple[Optional[str], str, str]
    """Return ``(error, source_path, final_path)`` with validated paths."""
    if not isinstance(request, RollbackRequest):
        return "rollback request is required", "", ""
    try:
        paths.job_prefix(request.job_id)
        paths.validate_tool_id(request.tool_id)
        paths.source_basename(request.source_path)
    except (paths.PathValidationError, TypeError) as exc:
        return str(exc), "", ""
    if not str(request.final_path or "").strip():
        return "final path is required", "", ""
    if request.config_choice not in CONFIG_CHOICES:
        return "config choice must be keep or restore", "", ""
    source_parent = os.path.dirname(
        os.path.abspath(os.path.normpath(request.source_path))
    )
    if not os.path.isdir(source_parent):
        return "source parent directory does not exist", "", ""
    final_path = os.path.abspath(os.path.normpath(request.final_path))
    return "", os.path.abspath(os.path.normpath(request.source_path)), final_path


def _source_kind(ops, source_path):
    # type: (object, str) -> Tuple[str, str]
    """Return ``(kind, link_target)`` without following reparse points."""
    if not ops.exists(source_path):
        return SOURCE_KIND_MISSING, ""
    try:
        if ops.is_junction(source_path):
            return SOURCE_KIND_JUNCTION, ops.read_link_target(source_path) or ""
        if ops.is_link(source_path):
            return SOURCE_KIND_LINK, ops.read_link_target(source_path) or ""
    except OSError:
        return SOURCE_KIND_OTHER, ""
    if ops.is_dir(source_path):
        return SOURCE_KIND_DIR, ""
    if os.path.isfile(source_path):
        return SOURCE_KIND_FILE, ""
    return SOURCE_KIND_OTHER, ""


def _target_state(ops, final_path, job_id):
    # type: (object, str, str) -> Tuple[bool, bool, int, int]
    """Return ``(exists, owned, bytes, files)`` for the recorded target."""
    if not ops.exists(final_path):
        return False, False, 0, 0
    if ops.is_link(final_path) or not ops.is_dir(final_path):
        return True, False, 0, 0
    owned = marker.owns_directory(final_path, job_id)
    total_bytes = 0
    file_count = 0
    try:
        from ..migrate.copy import sample_tree

        total_bytes, file_count = sample_tree(final_path)
    except OSError:
        pass
    return True, owned, total_bytes, file_count


def _free_space(free_space_provider, directory, warnings):
    # type: (object, str, List[str]) -> Optional[int]
    if free_space_provider is not None:
        try:
            value = free_space_provider(directory)
        except (OSError, TypeError, ValueError) as exc:
            warnings.append("free-space check failed: %s" % exc)
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            warnings.append("free-space check returned an unusable value")
            return None
        return value
    try:
        return int(shutil.disk_usage(directory).free)
    except OSError:
        warnings.append("free-space check is unavailable on this platform")
        return None


def _blocked(precheck, code, message):
    # type: (RollbackPrecheck, str, str) -> RollbackPrecheck
    precheck.ok = False
    precheck.mode = MODE_BLOCKED
    precheck.error_code = code
    precheck.error_message = message
    return precheck


def precheck_rollback(
    request,
    ops=None,
    process_provider=None,
    free_space_provider=None,
    jobs_dir=None,
):
    # type: (RollbackRequest, object, object, object, Optional[str]) -> RollbackPrecheck
    """Observe the filesystem and decide whether a rollback may start.

    Nothing is written.  Each early return maps to one AC: target unavailable
    (AC-ROL-05), insufficient space (AC-ROL-02), already rolled back, residual
    copy needing confirmation (AC-ROL-08) and contradictory evidence
    (state abnormal).
    """
    ops = ops or MigrationOps()
    precheck = RollbackPrecheck(ok=False)
    error, source_path, final_path = _normalise_request(request)
    if error:
        return _blocked(precheck, ERROR_INVALID_INPUT, error)

    job_id = (request.job_id or "").lower()
    recorded_backup = ""

    if jobs_dir:
        store = JournalStore(jobs_dir)
        migration = store.load(request.job_id, TYPE_MIGRATION)
        for warning in store.warnings:
            precheck.warnings.append(str(warning.get("message", "")))
        if migration is None:
            return _blocked(
                precheck,
                ERROR_RECOVERY,
                "the original migration journal is missing or unreadable",
            )
        if migration.tool_id and migration.tool_id != request.tool_id:
            return _blocked(
                precheck, ERROR_INVALID_INPUT, "journal tool id does not match"
            )
        if migration.source and not paths.same_path(
            migration.source, source_path
        ):
            return _blocked(
                precheck, ERROR_INVALID_INPUT, "journal source does not match"
            )
        if migration.final and not paths.same_path(migration.final, final_path):
            return _blocked(
                precheck, ERROR_INVALID_INPUT, "journal target does not match"
            )
        recorded_backup = migration.backup_dir or ""
        if migration.state not in ("DONE", "LINKED_CLEANUP_PENDING"):
            precheck.warnings.append(
                "migration journal state is %s, not DONE" % migration.state
            )

    precheck.backup_dir = request.backup_dir or recorded_backup

    kind, link_target = _source_kind(ops, source_path)
    exists, owned, target_bytes, target_files = _target_state(
        ops, final_path, job_id
    )

    precheck.source_exists = kind != SOURCE_KIND_MISSING
    precheck.source_kind = kind
    precheck.source_link_target = link_target
    precheck.source_link_matches = bool(
        link_target and link_target_matches(link_target, final_path)
    )
    precheck.target_exists = exists
    precheck.target_owned = owned
    precheck.target_bytes = target_bytes
    precheck.target_files = target_files

    required = request.expected_bytes
    if isinstance(required, bool) or not isinstance(required, int) or required < 0:
        required = target_bytes
    precheck.required_bytes = required

    if kind == SOURCE_KIND_JUNCTION:
        if not exists:
            return _blocked(
                precheck,
                ERROR_TARGET_UNAVAILABLE,
                "the target disk is unavailable; the junction was left in place",
            )
        if not owned:
            return _blocked(
                precheck,
                ERROR_STATE,
                "the target does not carry this job's ownership marker",
            )
        if not precheck.source_link_matches:
            return _blocked(
                precheck,
                ERROR_STATE,
                "the junction points somewhere else; neither side was touched",
            )
        hit = detect_processes(
            [_ToolStub(request.tool_id, request.process_names)],
            process_provider=process_provider,
        ).get(request.tool_id)
        if hit is not None:
            precheck.process = hit.to_dict()
            if hit.blocked_by_process:
                return _blocked(
                    precheck,
                    ERROR_PROCESS_BUSY,
                    "the tool is running or its process state is unknown",
                )
        free = _free_space(
            free_space_provider,
            os.path.dirname(source_path),
            precheck.warnings,
        )
        precheck.free_bytes = free
        if free is not None and free < required:
            precheck.free_space_ok = False
            return _blocked(
                precheck,
                ERROR_INSUFFICIENT_SPACE,
                "insufficient space: need %d bytes, %d available, gap %d"
                % (required, free, max(0, required - free)),
            )
        precheck.ok = True
        precheck.mode = MODE_ROLLBACK
        return precheck

    if kind == SOURCE_KIND_DIR:
        if not exists:
            precheck.ok = True
            precheck.mode = MODE_ALREADY_ROLLED_BACK
            return precheck
        if not owned:
            return _blocked(
                precheck,
                ERROR_STATE,
                "the source is a plain directory and the target is not owned "
                "by this job; nothing was touched",
            )
        precheck.ok = True
        precheck.mode = MODE_RESIDUAL_COPY
        if not request.confirm_residual_delete:
            precheck.error_message = (
                "the source is already a real directory; a residual target "
                "copy exists and needs explicit confirmation before deletion"
            )
        return precheck

    if kind == SOURCE_KIND_MISSING:
        if exists and owned:
            return _blocked(
                precheck,
                ERROR_RECOVERY,
                "the source is missing while the target still exists; "
                "automatic rollback cannot decide safely",
            )
        return _blocked(
            precheck,
            ERROR_STATE,
            "neither the source nor an owned target can be found",
        )

    if kind == SOURCE_KIND_LINK:
        return _blocked(
            precheck,
            ERROR_STATE,
            "the source is a non-junction link; it will not be removed",
        )

    return _blocked(
        precheck,
        ERROR_STATE,
        "the source is not a directory or a junction; nothing was touched",
    )


class _Journal(object):
    """Thin wrapper: journal writes never abort a rollback mid-flight."""

    def __init__(self, jobs_dir, warnings):
        # type: (Optional[str], List[str]) -> None
        self.store = JournalStore(jobs_dir) if jobs_dir else None
        self.warnings = warnings

    def _warn(self, action, exc):
        # type: (str, object) -> None
        self.warnings.append("journal %s failed: %s" % (action, exc))

    def open(self, request, restore_dir, backup_dir, payload):
        # type: (RollbackRequest, str, str, Dict[str, object]) -> None
        if self.store is None:
            return
        try:
            existing = self.store.load(request.job_id, TYPE_ROLLBACK)
            if existing is None:
                self.store.begin(
                    request.job_id,
                    entry_type=TYPE_ROLLBACK,
                    tool_id=request.tool_id,
                    source=request.source_path,
                    final=request.final_path,
                    restore_dir=restore_dir,
                    backup_dir=backup_dir,
                    state=STATE_ROLLBACK_PLANNED,
                    step=STAGE_PRECHECK,
                    intent=payload,
                )
            else:
                self.store.set_intent(
                    request.job_id,
                    STAGE_PRECHECK,
                    payload,
                    state=STATE_ROLLBACK_PLANNED,
                    entry_type=TYPE_ROLLBACK,
                )
        except (StoreError, OSError) as exc:
            self._warn("open", exc)

    def intent(self, request, step, payload):
        # type: (RollbackRequest, str, Dict[str, object]) -> None
        if self.store is None:
            return
        try:
            self.store.set_intent(
                request.job_id,
                step,
                payload,
                entry_type=TYPE_ROLLBACK,
            )
        except (StoreError, OSError) as exc:
            self._warn("intent %s" % step, exc)

    def result(self, request, state, payload=None, step=None):
        # type: (RollbackRequest, str, object, Optional[str]) -> None
        if self.store is None:
            return
        try:
            self.store.set_result(
                request.job_id,
                state,
                payload=payload,
                step=step,
                entry_type=TYPE_ROLLBACK,
            )
        except (StoreError, OSError) as exc:
            self._warn("result %s" % state, exc)


def _step(result, stage, status, **fields):
    # type: (RollbackResult, str, str, object) -> None
    record = {"stage": stage, "status": status}
    record.update(fields)
    result.steps.append(record)


def _compile_failure(result):
    # type: (RollbackResult) -> None
    result.diagnostics.append(result.error_message)
    result.manual_steps = [
        "Keep both the source and target directories; do not delete either.",
        "Check the rollback journal and the transfer log in the restore directory.",
        "Retry the rollback after the reported cause has been resolved.",
        "If both copies exist, compare them manually before choosing one.",
    ]


def _relink(ops, request):
    # type: (object, RollbackRequest) -> bool
    """Restore the junction so the source keeps working after a failed copy."""
    try:
        link_step.ensure_link(ops, request.source_path, request.final_path)
        check = link_step.verify_link(
            ops, request.source_path, request.final_path
        )
        return bool(check.ok)
    except (link_step.LinkStepError, OSError):
        return False


def _remove_restore_dir(restore_parent, source_parent, job_id, warnings):
    # type: (str, str, str, List[str]) -> None
    try:
        errors = cleanup.remove_tree(
            restore_parent,
            source_parent,
            lambda name: paths.is_restore_dir_name(name, job_id),
        )
    except (cleanup.UnsafeCleanupError, OSError) as exc:
        warnings.append("cannot remove restore directory: %s" % exc)
        return
    for error in errors:
        warnings.append("cannot remove restore directory: %s" % error)


def _content_check(left, right, cancel_token=None, ignore_paths=()):
    # type: (str, str, object, tuple) -> Optional[str]
    """Return ``None`` only when ``left`` and ``right`` provably hold equal bytes.

    Any other outcome -- a size/content mismatch, a read or access error, a
    cancellation -- returns a human-readable reason.  Callers must treat a
    reason as "not verified" and keep the data (T26/R-02).
    """
    try:
        outcome = verify_trees(
            left,
            right,
            cancel_token=cancel_token,
            ignore_paths=ignore_paths,
        )
    except MigrationCancelled:
        return "content verification was cancelled"
    except OSError as exc:
        return "content verification is unavailable: %s" % exc
    if outcome.ok:
        return None
    return outcome.reason or "content verification did not pass"


def _ignored_ownership_files(job_id):
    # type: (str) -> tuple
    """Relative paths excluded from target-vs-source content comparison.

    The target carries the job's ownership marker while the restored copy has
    it stripped, so the marker is bookkeeping, not user data.
    """
    try:
        return (paths.owner_marker_name(job_id),)
    except (paths.PathValidationError, TypeError):
        return ()


def _strip_marker(restore_child, job_id):
    # type: (str, str) -> bool
    """Remove the job marker from the restored copy before promoting it."""
    handle = marker.marker_path(restore_child, job_id)
    if not os.path.lexists(handle):
        return True
    try:
        os.unlink(handle)
    except OSError:
        return False
    return True


def _readable_directory(path):
    # type: (str) -> bool
    try:
        with os.scandir(path) as entries:
            list(entries)
    except OSError:
        return False
    return True


def _record_outcome(
    result, request, journal, history_path, learned_path, data_bytes
):
    # type: (RollbackResult, RollbackRequest, _Journal, Optional[str], Optional[str], int) -> None
    if history_path:
        try:
            store = HistoryStore(history_path)
            store.record_rollback(
                tool=request.tool_id,
                item=paths.source_basename(request.source_path),
                source=request.source_path,
                target=request.final_path,
                size=max(0, int(data_bytes)),
                result=RESULT_ROLLED_BACK,
                job_id=request.job_id,
                stage=STAGE_RECORD,
                reason=result.error_message if result.error_code else "",
            )
            result.history_recorded = True
        except (StoreError, OSError) as exc:
            result.warnings.append("history was not recorded: %s" % exc)

    if learned_path:
        try:
            rules = LearnedRulesStore(learned_path)
            rules.mark_red(
                item_id_for(request.tool_id, source_path=request.source_path),
                reason="rolled_back",
                rule_id=ROLLBACK_RULE_ID,
            )
            result.learned_updated = True
        except (StoreError, OSError, ValueError) as exc:
            result.warnings.append("learned rule was not updated: %s" % exc)

    journal.result(
        request,
        STATE_ROLLED_BACK,
        payload={
            "state": STATE_ROLLED_BACK,
            "target_removed": result.target_removed,
            "residual_copy": result.residual_copy,
            "config": result.config.to_dict() if result.config else None,
        },
        step=STAGE_RECORD,
    )


def _finish_failure(result, request, journal, ops, allow_relink, source_parent, job_id):
    # type: (RollbackResult, RollbackRequest, _Journal, object, bool, str, str) -> RollbackResult
    """Keep both copies and, when needed, make the source usable again."""
    result.data_kept = True
    if result.restore_dir:
        _remove_restore_dir(
            result.restore_dir, source_parent, job_id, result.warnings
        )
    if allow_relink and not ops.exists(request.source_path):
        result.relinked = _relink(ops, request)
        if not result.relinked:
            result.state = STATE_ROLLBACK_RECOVERY_REQUIRED
            result.error_code = ERROR_RECOVERY
            result.error_message = (
                "%s; the junction could not be recreated either"
                % result.error_message
            )
            journal.result(
                request,
                STATE_ROLLBACK_RECOVERY_REQUIRED,
                payload={
                    "state": STATE_ROLLBACK_RECOVERY_REQUIRED,
                    "error_code": result.error_code,
                    "error_message": result.error_message,
                },
            )
            _compile_failure(result)
            return result
    result.state = STATE_ROLLBACK_FAILED
    journal.result(
        request,
        STATE_ROLLBACK_FAILED,
        payload={
            "state": STATE_ROLLBACK_FAILED,
            "error_code": result.error_code,
            "error_message": result.error_message,
            "relinked": result.relinked,
        },
    )
    _compile_failure(result)
    return result


def rollback(
    request,
    transfer=None,
    cancel_token=None,
    progress_cb=None,
    ops=None,
    process_provider=None,
    free_space_provider=None,
    jobs_dir=None,
    history_path=None,
    learned_path=None,
    sample_seconds=DEFAULT_SAMPLE_SECONDS,
):
    # type: (RollbackRequest, object, object, object, object, object, object, Optional[str], Optional[str], Optional[str], float) -> RollbackResult
    """Roll one migrated item back to its original location.

    ``ops``, ``transfer``, ``process_provider`` and ``free_space_provider``
    are injectable for tests and fault drills.  ``jobs_dir``, ``history_path``
    and ``learned_path`` are optional: when supplied, the engine writes the
    rollback journal, appends one history record and marks the item red.
    """
    ops = ops or MigrationOps()
    emitter = Emitter(progress_cb, request.job_id if request else "", getattr(request, "tool_id", ""))
    result = RollbackResult(
        job_id=getattr(request, "job_id", "") or "",
        tool_id=getattr(request, "tool_id", "") or "",
        source_path=getattr(request, "source_path", "") or "",
        final_path=getattr(request, "final_path", "") or "",
    )
    journal = _Journal(jobs_dir, result.warnings)

    emitter.emit(STAGE_PRECHECK, STATUS_START)
    precheck = precheck_rollback(
        request,
        ops=ops,
        process_provider=process_provider,
        free_space_provider=free_space_provider,
        jobs_dir=jobs_dir,
    )
    result.precheck = precheck
    result.process = precheck.process
    result.backup_dir = precheck.backup_dir
    result.warnings.extend(precheck.warnings)
    result.source_path = getattr(request, "source_path", "") or ""
    result.final_path = getattr(request, "final_path", "") or ""
    if precheck.mode == MODE_ALREADY_ROLLED_BACK:
        result.state = STATE_ALREADY_ROLLED_BACK
        result.data_kept = True
        result.error_message = "the item is already rolled back"
        emitter.emit(STAGE_PRECHECK, STATUS_DONE)
        return result
    if not precheck.ok and precheck.mode == MODE_BLOCKED:
        result.blocked = True
        result.data_kept = True
        result.error_code = precheck.error_code
        result.error_message = precheck.error_message
        result.state = (
            STATE_STATE_ABNORMAL
            if precheck.error_code == ERROR_STATE
            else STATE_ROLLBACK_FAILED
        )
        emitter.emit(
            STAGE_PRECHECK,
            STATUS_FAILED,
            error_code=precheck.error_code,
            message=precheck.error_message,
        )
        _step(result, STAGE_PRECHECK, STATUS_FAILED, error_code=precheck.error_code)
        return result
    emitter.emit(STAGE_PRECHECK, STATUS_DONE)

    source_path = result.source_path
    final_path = result.final_path
    job_id = (request.job_id or "").lower()
    source_parent = os.path.dirname(source_path)
    restore_parent = paths.restore_path(source_path, request.job_id)
    restore_child = os.path.join(restore_parent, paths.source_basename(source_path))
    log_path = request.log_path or os.path.join(restore_parent, LOG_NAME)
    result.restore_dir = restore_parent

    if precheck.mode == MODE_RESIDUAL_COPY:
        result.state = STATE_RESIDUAL_COPY
        result.residual_copy = True
        result.data_kept = True
        result.error_message = precheck.error_message
        if not request.confirm_residual_delete:
            result.blocked = True
            return result
        journal.open(
            request,
            restore_parent,
            precheck.backup_dir,
            {"action": "remove_residual_copy", "target": final_path},
        )
        journal.intent(
            request, STAGE_CLEANUP, {"action": "remove_residual_copy", "target": final_path}
        )
        # T26/R-02: the residual copy is only discarded once its bytes are
        # proven equal to the source that stays.  An unavailable or failed
        # check counts as "not verified" and keeps the copy.
        residual_reason = _content_check(
            final_path,
            source_path,
            cancel_token,
            _ignored_ownership_files(job_id),
        )
        if residual_reason:
            errors = [
                "refusing to delete the residual target copy: %s"
                % residual_reason
            ]
        else:
            try:
                errors = cleanup.remove_owned_tree(
                    final_path, os.path.dirname(final_path), job_id
                )
            except (cleanup.UnsafeCleanupError, OSError) as exc:
                errors = [str(exc)]
        if errors:
            result.error_code = ERROR_TARGET_DELETE
            if residual_reason:
                result.error_code = ERROR_VERIFY
            result.error_message = "; ".join(errors[:MAX_DETAIL_ITEMS])
            result.warnings.extend(errors)
        else:
            result.target_removed = True
            result.residual_copy = False
            result.error_message = "the residual target copy was removed"
        journal.result(
            request,
            STATE_RESIDUAL_COPY,
            payload={
                "state": STATE_RESIDUAL_COPY,
                "target_removed": result.target_removed,
            },
        )
        return result

    # Preconditions are satisfied: record the intent before the first mutation.
    journal.open(
        request,
        restore_parent,
        precheck.backup_dir,
        {
            "action": "rollback",
            "source": source_path,
            "target": final_path,
            "target_bytes": precheck.target_bytes,
            "target_files": precheck.target_files,
        },
    )
    if is_cancelled(cancel_token):
        result.error_code = ERROR_CANCELLED
        result.error_message = "rollback cancelled before any change"
        _compile_failure(result)
        return result

    # 1. Remove only the verified junction; nothing else is touched.
    emitter.emit(STAGE_LINK_REMOVE, STATUS_START)
    journal.intent(
        request,
        STAGE_LINK_REMOVE,
        {"link": source_path, "target": final_path},
    )
    try:
        link_step.remove_own_link(ops, source_path, final_path)
    except link_step.LinkStepError as exc:
        result.blocked = True
        result.data_kept = True
        result.error_code = ERROR_LINK_REMOVE
        result.error_message = str(exc)
        result.state = STATE_STATE_ABNORMAL
        journal.result(
            request,
            STATE_STATE_ABNORMAL,
            payload={"error_code": ERROR_LINK_REMOVE, "message": str(exc)},
        )
        _compile_failure(result)
        emitter.emit(STAGE_LINK_REMOVE, STATUS_FAILED)
        _step(result, STAGE_LINK_REMOVE, STATUS_FAILED)
        return result
    result.link_removed = not ops.exists(source_path)
    journal.result(
        request,
        STATE_ROLLBACK_LINK_REMOVED,
        payload={"link_removed": result.link_removed},
        step=STAGE_COPY,
    )
    emitter.emit(STAGE_LINK_REMOVE, STATUS_DONE)

    if ops.exists(source_path):
        result.state = STATE_ROLLBACK_RECOVERY_REQUIRED
        result.data_kept = True
        result.error_code = ERROR_STATE
        result.error_message = (
            "the source path is occupied after removing the junction"
        )
        journal.result(
            request,
            STATE_ROLLBACK_RECOVERY_REQUIRED,
            payload={"error_code": ERROR_STATE, "message": result.error_message},
        )
        _compile_failure(result)
        return result

    # A leftover restore slot from an interrupted attempt is job-scoped.
    if os.path.lexists(restore_parent):
        _remove_restore_dir(restore_parent, source_parent, job_id, result.warnings)
        if os.path.lexists(restore_parent):
            return _finish_failure(
                _fail(
                    result,
                    ERROR_RECOVERY,
                    "a leftover restore directory could not be removed",
                ),
                request,
                journal,
                ops,
                True,
                source_parent,
                job_id,
            )
    try:
        os.makedirs(restore_parent)
    except OSError as exc:
        return _finish_failure(
            _fail(result, ERROR_COPY, "cannot create restore directory: %s" % exc),
            request,
            journal,
            ops,
            True,
            source_parent,
            job_id,
        )

    # 2. Copy the target back into the same-volume restore slot.
    emitter.emit(STAGE_COPY, STATUS_START)
    journal.intent(
        request,
        STAGE_COPY,
        {"source": final_path, "staging": restore_child, "log": log_path},
    )
    try:
        copy_result = copy_to_staging(
            final_path,
            restore_child,
            log_path,
            cancel_token=cancel_token,
            transfer=transfer,
            progress_cb=progress_cb,
            job_id=request.job_id,
            tool_id=request.tool_id,
            sample_seconds=sample_seconds,
            expected_bytes=precheck.target_bytes,
            expected_files=precheck.target_files,
        )
        result.copy = copy_result
    except MigrationCancelled:
        return _finish_failure(
            _fail(result, ERROR_CANCELLED, "rollback cancelled during copy"),
            request,
            journal,
            ops,
            True,
            source_parent,
            job_id,
        )
    except (OSError, StoreError) as exc:
        return _finish_failure(
            _fail(result, ERROR_COPY, "copy failed: %s" % exc),
            request,
            journal,
            ops,
            True,
            source_parent,
            job_id,
        )
    if not copy_result.ok:
        return _finish_failure(
            _fail(
                result,
                ERROR_COPY,
                "the copy back exited with %d" % copy_result.exit_code,
            ),
            request,
            journal,
            ops,
            True,
            source_parent,
            job_id,
        )
    journal.result(
        request,
        STATE_ROLLBACK_COPIED,
        payload={"exit_code": copy_result.exit_code},
        step=STAGE_VERIFY,
    )
    emitter.emit(STAGE_COPY, STATUS_DONE)

    # 3. Authoritative V2 verification of the restored copy.
    emitter.emit(STAGE_VERIFY, STATUS_START)
    journal.intent(
        request,
        STAGE_VERIFY,
        {"source": final_path, "target": restore_child},
    )
    try:
        verify_result = verify_trees(
            final_path, restore_child, cancel_token=cancel_token
        )
    except MigrationCancelled:
        return _finish_failure(
            _fail(result, ERROR_CANCELLED, "rollback cancelled during verification"),
            request,
            journal,
            ops,
            True,
            source_parent,
            job_id,
        )
    result.verify = verify_result
    if not verify_result.ok:
        return _finish_failure(
            _fail(
                result,
                ERROR_VERIFY,
                verify_result.reason or "the restored copy did not verify",
            ),
            request,
            journal,
            ops,
            True,
            source_parent,
            job_id,
        )
    if not _strip_marker(restore_child, job_id):
        return _finish_failure(
            _fail(
                result,
                ERROR_RESTORE,
                "cannot remove the ownership marker from the restored copy",
            ),
            request,
            journal,
            ops,
            True,
            source_parent,
            job_id,
        )
    journal.result(
        request,
        STATE_ROLLBACK_VERIFIED,
        payload={
            "file_count": verify_result.target_file_count,
            "total_bytes": verify_result.target_bytes,
        },
        step=STAGE_RESTORE,
    )
    emitter.emit(STAGE_VERIFY, STATUS_DONE)

    # 4. Promote the verified copy with a same-volume atomic rename.
    emitter.emit(STAGE_RESTORE, STATUS_START)
    journal.intent(
        request,
        STAGE_RESTORE,
        {"staging": restore_child, "source": source_path},
    )
    if ops.exists(source_path):
        return _finish_failure(
            _fail(
                result,
                ERROR_RESTORE,
                "the source path was recreated before it could be restored",
            ),
            request,
            journal,
            ops,
            False,
            source_parent,
            job_id,
        )
    if not ops.same_volume(restore_child, source_path):
        return _finish_failure(
            _fail(result, ERROR_RESTORE, "restore and source are on different volumes"),
            request,
            journal,
            ops,
            False,
            source_parent,
            job_id,
        )
    try:
        ops.rename(restore_child, source_path)
    except OSError as exc:
        return _finish_failure(
            _fail(
                result,
                ERROR_RESTORE,
                "cannot move the restored copy into place: %s" % exc,
            ),
            request,
            journal,
            ops,
            False,
            source_parent,
            job_id,
        )
    result.source_restored = True
    journal.result(
        request,
        STATE_ROLLBACK_SOURCE_READY,
        payload={"source_restored": True},
        step=STAGE_CONFIG,
    )
    emitter.emit(STAGE_RESTORE, STATUS_DONE)
    if not _readable_directory(source_path):
        result.warnings.append("the restored source directory is not readable")

    # 5. Configuration restore; failures never change the data outcome.
    emitter.emit(STAGE_CONFIG, STATUS_START)
    journal.intent(request, STAGE_CONFIG, {"backup_dir": precheck.backup_dir})
    config_outcome = restore_configs(
        precheck.backup_dir,
        choice=request.config_choice,
        expected_tool_id=request.tool_id,
        expected_job_id=request.job_id,
    )
    result.config = config_outcome
    if not config_outcome.ok:
        result.warnings.append(
            "configuration restore is incomplete: %s" % config_outcome.reason
        )
    journal.result(
        request,
        STATE_ROLLBACK_CONFIG_RESTORED,
        payload={"config": config_outcome.to_dict()},
        step=STAGE_CLEANUP,
    )
    emitter.emit(STAGE_CONFIG, STATUS_DONE)

    # 6. Delete only the recorded, marker-owned target; a failure leaves a
    #    residual copy and never invalidates the restored source.
    emitter.emit(STAGE_CLEANUP, STATUS_START)
    journal.intent(
        request,
        STAGE_CLEANUP,
        {"target": final_path, "owned": True},
    )
    # T26/R-02: re-check content immediately before deleting the target copy.
    # The restored source is already in place, so a target that no longer
    # matches it is kept as a residual copy instead of being deleted.
    predelete_reason = _content_check(
        final_path,
        source_path,
        cancel_token,
        _ignored_ownership_files(job_id),
    )
    if predelete_reason:
        result.residual_copy = True
        result.error_code = ERROR_VERIFY
        result.error_message = (
            "refusing to delete the target copy: %s" % predelete_reason
        )
        result.warnings.append(result.error_message)
    else:
        try:
            errors = cleanup.remove_owned_tree(
                final_path, os.path.dirname(final_path), job_id
            )
        except (cleanup.UnsafeCleanupError, OSError) as exc:
            errors = [str(exc)]
        if errors:
            result.residual_copy = True
            result.error_code = ERROR_TARGET_DELETE
            result.error_message = "; ".join(errors[:MAX_DETAIL_ITEMS])
            result.warnings.extend(errors)
        else:
            result.target_removed = True
    emitter.emit(
        STAGE_CLEANUP,
        STATUS_DONE if result.target_removed else STATUS_FAILED,
    )
    if os.path.lexists(restore_parent):
        _remove_restore_dir(restore_parent, source_parent, job_id, result.warnings)

    result.state = STATE_ROLLED_BACK
    result.data_kept = True
    _record_outcome(
        result,
        request,
        journal,
        history_path,
        learned_path,
        precheck.target_bytes,
    )
    _step(result, STAGE_RECORD, STATUS_DONE)
    return result


def _fail(result, code, message):
    # type: (RollbackResult, str, str) -> RollbackResult
    result.error_code = code
    result.error_message = message
    result.state = STATE_ROLLBACK_FAILED
    return result


__all__ = [
    "ROLLBACK_RULE_ID",
    "precheck_rollback",
    "rollback",
]
