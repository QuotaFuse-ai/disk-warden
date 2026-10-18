# -*- coding: utf-8 -*-
"""Migration engine B: promote, swap, link, verify and clean up (S5-S9).

Consumes the verified ``MigrationResult`` produced by engine A and never
copies data again.  The source directory is never deleted recursively: it is
renamed to ``.dwtombstone-<job8>`` beside itself (S6), the junction is created
in its place (S7), and the tombstone is removed only after the junction has
been verified (S8/S9, architecture 6.1).

Every step is an idempotent ``ensure_*`` call.  Failures roll back to a state
where the source data is present at the source path; when the evidence is not
conclusive the job lands in ``RECOVERY_REQUIRED`` and nothing more is touched
(architecture 6.3/6.4/6.5).

Python 3.8 compatible (decision 0005).
"""

import os

from ..scan.cancel import is_cancelled
from ..scan.process import detect_processes
from . import link, marker, paths, swap
from .cleanup import UnsafeCleanupError, remove_tombstone
from .models import (
    ERROR_CANCELLED,
    ERROR_CLEANUP,
    ERROR_INPUT,
    ERROR_LINK,
    ERROR_LINK_VERIFY,
    ERROR_PROCESS_BUSY,
    ERROR_RECOVERY,
    ERROR_SWAP,
    STAGE_CLEANUP,
    STAGE_LINK,
    STAGE_LINK_VERIFY,
    STAGE_SWAP,
    STATE_DONE,
    STATE_FAILED,
    STATE_LINKED,
    STATE_LINKED_CLEANUP_PENDING,
    STATE_RECOVERY_REQUIRED,
    STATE_ROLLED_BACK,
    STATE_SOURCE_RENAMED,
    STATE_TARGET_READY,
    STATE_VERIFIED_LINK,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PROGRESS,
    STATUS_START,
    CompletionResult,
)
from .ops import MigrationOps
from .progress import Emitter


class _ToolStub(object):
    """Minimal catalog-tool shape expected by ``detect_processes``."""

    def __init__(self, tool_id, process_names):
        # type: (str, object) -> None
        self.id = tool_id
        self.processes = tuple(process_names or ())


def _process_hit(tool_id, process_names, provider):
    # type: (str, object, object) -> object
    hits = detect_processes(
        [_ToolStub(tool_id, process_names)], process_provider=provider
    )
    return hits.get(tool_id)


def _step(result, name, ok, **fields):
    # type: (CompletionResult, str, bool, object) -> None
    payload = {"step": name, "ok": bool(ok)}
    payload.update(fields)
    result.steps.append(payload)


def _fail(result, code, message, state=STATE_FAILED, **fields):
    # type: (CompletionResult, str, str, str, object) -> CompletionResult
    result.error_code = code
    result.error_message = message
    result.state = state
    for key, value in fields.items():
        setattr(result, key, value)
    return result


def _discard_owned_target(result, ops, final_path, job_id):
    # type: (CompletionResult, object, str, str) -> list
    """Remove a promoted target only when this job's marker proves ownership."""
    if not ops.exists(final_path):
        return []
    if ops.is_link(final_path) or not ops.is_dir(final_path):
        return []
    if not marker.owns_directory(final_path, job_id):
        return []
    try:
        errors = swap.discard_target(final_path, job_id)
    except (UnsafeCleanupError, OSError) as exc:
        errors = [str(exc)]
    if errors:
        result.cleanup_errors.extend(errors)
    _step(result, "discard-target", not errors)
    return errors


def _discard_staging(result, staging_path, job_id):
    # type: (CompletionResult, str, str) -> list
    if not staging_path or not os.path.lexists(staging_path):
        return []
    try:
        errors = swap.discard_staging(staging_path, job_id)
    except (UnsafeCleanupError, OSError) as exc:
        errors = [str(exc)]
    if errors:
        result.cleanup_errors.extend(errors)
    _step(result, "discard-staging", not errors)
    return errors


def complete_migration(
    result,
    process_names=None,
    process_provider=None,
    ops=None,
    progress_cb=None,
    cancel_token=None,
):
    # type: (object, object, object, object, object, object) -> CompletionResult
    """Run S5-S9 on a verified staging copy and return a terminal result.

    ``process_names`` are the tool's executable names from the catalog; they
    are matched before the source is touched (architecture 6.1.8).  An empty
    list means "the catalog defines no processes"; an enumeration failure is
    treated as blocking.

    Cancellation is honoured before S5 and again before S6.  Once the source
    has been renamed, cancellation is disabled until the junction is verified
    (architecture 6.8); afterwards cleanup always runs to completion.
    """
    ops = ops or MigrationOps()
    job_id = result.job_id or ""
    tool_id = result.tool_id or ""
    source_path = os.path.abspath(os.path.normpath(result.source_path or ""))
    final_path = os.path.abspath(os.path.normpath(result.final_path or ""))
    staging_path = os.path.abspath(os.path.normpath(result.staging_path or ""))

    completion = CompletionResult(
        job_id=job_id,
        tool_id=tool_id,
        source_path=source_path,
        final_path=final_path,
        staging_path=staging_path,
        tombstone_path="",
    )
    emitter = Emitter(progress_cb, job_id, tool_id)
    emitter.emit(
        STAGE_SWAP,
        STATUS_START,
        source_path=source_path,
        final_path=final_path,
        staging_path=staging_path,
    )

    if not result.ok or result.verify is None or not result.verify.ok:
        return _fail(
            completion,
            ERROR_INPUT,
            "prepare_copy did not produce a verified staging copy",
        )

    try:
        paths.job_prefix(job_id)
        completion.tombstone_path = paths.tombstone_path(source_path, job_id)
    except (paths.PathValidationError, ValueError) as exc:
        return _fail(completion, ERROR_INPUT, str(exc))

    if not ops.is_dir(source_path) or ops.is_link(source_path):
        return _fail(
            completion,
            ERROR_INPUT,
            "source is not a plain directory: %s" % source_path,
        )

    staging_exists = ops.exists(staging_path)
    if staging_exists and (
        ops.is_link(staging_path) or not ops.is_dir(staging_path)
    ):
        return _fail(
            completion,
            ERROR_INPUT,
            "staging is not a plain directory: %s" % staging_path,
        )
    if not staging_exists and not ops.exists(final_path):
        return _fail(
            completion,
            ERROR_SWAP,
            "neither staging nor the final target exists",
        )

    # S6 precondition, executed before the first mutation so a busy tool
    # leaves the source and the staging copy untouched (architecture 6.1.8).
    hit = _process_hit(tool_id, process_names, process_provider)
    if hit is not None:
        completion.process = hit.to_dict()
    if hit is not None and hit.blocked_by_process:
        _discard_owned_target(completion, ops, final_path, job_id)
        _discard_staging(completion, staging_path, job_id)
        emitter.emit(
            STAGE_SWAP,
            STATUS_FAILED,
            error_code=ERROR_PROCESS_BUSY,
            process_status=hit.status,
        )
        return _fail(
            completion,
            ERROR_PROCESS_BUSY,
            "tool is still running (%s); source was not touched" % hit.status,
        )
    _step(
        completion,
        "process-check",
        True,
        status=hit.status if hit is not None else "unknown",
    )

    if is_cancelled(cancel_token):
        _discard_staging(completion, staging_path, job_id)
        emitter.emit(STAGE_SWAP, STATUS_FAILED, error_code=ERROR_CANCELLED)
        return _fail(
            completion,
            ERROR_CANCELLED,
            "cancelled before the target was promoted; source untouched",
        )

    # The ownership marker goes in after T09 verification and before the
    # rename, so verification counts stay comparable and later cleanup can
    # prove the target belongs to this job (architecture 6.5).
    if ops.exists(staging_path):
        try:
            marker.write_owner_marker(
                staging_path,
                job_id,
                tool_id,
                source_path,
                final_path,
                file_count=result.verify.source_file_count,
                total_bytes=result.verify.source_bytes,
            )
        except OSError as exc:
            _discard_staging(completion, staging_path, job_id)
            emitter.emit(
                STAGE_SWAP, STATUS_FAILED, error_code=ERROR_SWAP
            )
            return _fail(
                completion,
                ERROR_SWAP,
                "cannot write the ownership marker: %s" % exc,
            )
        _step(completion, "S5-marker", True)

    # S5: staging -> final, same volume, atomic.
    try:
        promoted, cleanup_errors = swap.ensure_target_ready(
            ops, staging_path, final_path, job_id
        )
    except swap.SwapError as exc:
        _discard_staging(completion, staging_path, job_id)
        emitter.emit(STAGE_SWAP, STATUS_FAILED, error_code=ERROR_SWAP)
        return _fail(completion, ERROR_SWAP, str(exc))
    completion.cleanup_errors.extend(cleanup_errors)
    # The leaf was renamed out of the staging parent; remove the now-empty
    # job directory (and the copy log) so no temporary residue remains.
    staging_cleanup = swap.discard_staging(staging_path, job_id)
    if staging_cleanup:
        completion.cleanup_errors.extend(staging_cleanup)
    _step(completion, "S5-cleanup-staging", not staging_cleanup)
    _step(completion, "S5", True, promoted=promoted)
    emitter.emit(
        STAGE_SWAP,
        STATUS_PROGRESS,
        step="S5",
        state=STATE_TARGET_READY,
        final_path=final_path,
    )

    if is_cancelled(cancel_token):
        _discard_owned_target(completion, ops, final_path, job_id)
        emitter.emit(STAGE_SWAP, STATUS_FAILED, error_code=ERROR_CANCELLED)
        return _fail(
            completion,
            ERROR_CANCELLED,
            "cancelled before the source swap; target removed, source untouched",
            state=STATE_ROLLED_BACK,
            rolled_back=True,
        )

    # S6: source -> tombstone, same parent and volume, atomic.
    try:
        renamed = swap.ensure_source_renamed(
            ops, source_path, completion.tombstone_path, job_id
        )
    except swap.SwapError as exc:
        if (
            ops.is_dir(source_path)
            and not ops.is_link(source_path)
            and not ops.exists(completion.tombstone_path)
        ):
            errors = _discard_owned_target(
                completion, ops, final_path, job_id
            )
            emitter.emit(STAGE_SWAP, STATUS_FAILED, error_code=ERROR_SWAP)
            return _fail(
                completion,
                ERROR_SWAP,
                str(exc),
                state=STATE_ROLLED_BACK,
                rolled_back=True,
                cleanup_errors=list(completion.cleanup_errors),
            )
        emitter.emit(
            STAGE_SWAP, STATUS_FAILED, error_code=ERROR_RECOVERY
        )
        return _fail(
            completion,
            ERROR_RECOVERY,
            str(exc),
            state=STATE_RECOVERY_REQUIRED,
        )
    _step(completion, "S6", True, renamed=renamed)
    emitter.emit(
        STAGE_SWAP,
        STATUS_DONE,
        state=STATE_SOURCE_RENAMED,
        tombstone_path=completion.tombstone_path,
    )

    # Cancellation stays disabled from here until the link is verified.
    emitter.emit(
        STAGE_LINK,
        STATUS_START,
        source_path=source_path,
        final_path=final_path,
    )
    try:
        created, target = link.ensure_link(ops, source_path, final_path)
    except link.LinkStepError as exc:
        completion.link_target = exc.target
        emitter.emit(STAGE_LINK, STATUS_FAILED, error_code=ERROR_LINK)
        if ops.exists(source_path) and ops.is_link(source_path):
            # Something this job cannot prove ownership of occupies the
            # source path; never remove it (architecture 6.5).
            return _fail(
                completion,
                ERROR_RECOVERY,
                str(exc),
                state=STATE_RECOVERY_REQUIRED,
            )
        try:
            swap.restore_source(
                ops, source_path, completion.tombstone_path, job_id
            )
            _step(completion, "S7-restore-source", True)
        except swap.SwapError as restore_exc:
            return _fail(
                completion,
                ERROR_RECOVERY,
                "%s; the source cannot be restored: %s"
                % (exc, restore_exc),
                state=STATE_RECOVERY_REQUIRED,
            )
        errors = _discard_owned_target(completion, ops, final_path, job_id)
        return _fail(
            completion,
            ERROR_LINK,
            str(exc),
            state=STATE_ROLLED_BACK,
            rolled_back=True,
            cleanup_errors=list(completion.cleanup_errors),
        )

    completion.link_created = created
    completion.link_target = target
    _step(completion, "S7", True, created=created, target=target)
    emitter.emit(
        STAGE_LINK,
        STATUS_DONE,
        state=STATE_LINKED,
        target=target,
        created=created,
    )

    # S8: read the target back and probe one file through the link.
    emitter.emit(
        STAGE_LINK_VERIFY,
        STATUS_START,
        source_path=source_path,
        target=completion.link_target,
    )
    try:
        check = link.verify_link(ops, source_path, final_path)
    except OSError as exc:
        check = link.LinkCheck(
            ok=False,
            target=completion.link_target or "",
            target_matches=False,
            reason="link verification failed: %s" % exc,
        )
    completion.link_verified = check.ok
    if check.target:
        completion.link_target = check.target
    _step(
        completion,
        "S8",
        check.ok,
        target=check.target,
        probe_rel=check.probe_rel,
        probe_kind=check.probe_kind,
        reason=check.reason,
    )
    if not check.ok:
        emitter.emit(
            STAGE_LINK_VERIFY,
            STATUS_FAILED,
            error_code=ERROR_LINK_VERIFY,
            reason=check.reason,
        )
        try:
            link.remove_own_link(ops, source_path, final_path)
            _step(completion, "S8-remove-link", True)
        except link.LinkStepError as exc:
            return _fail(
                completion,
                ERROR_RECOVERY,
                "%s; the junction cannot be removed: %s"
                % (check.reason, exc),
                state=STATE_RECOVERY_REQUIRED,
            )
        try:
            swap.restore_source(
                ops, source_path, completion.tombstone_path, job_id
            )
            _step(completion, "S8-restore-source", True)
        except swap.SwapError as exc:
            return _fail(
                completion,
                ERROR_RECOVERY,
                "%s; the source cannot be restored: %s"
                % (check.reason, exc),
                state=STATE_RECOVERY_REQUIRED,
            )
        return _fail(
            completion,
            ERROR_LINK_VERIFY,
            check.reason,
            state=STATE_ROLLED_BACK,
            rolled_back=True,
            link_created=False,
            link_verified=False,
        )

    emitter.emit(
        STAGE_LINK_VERIFY,
        STATUS_DONE,
        state=STATE_VERIFIED_LINK,
        target=check.target,
        probe_rel=check.probe_rel,
        probe_kind=check.probe_kind,
    )

    # S9: the link is proven, so the tombstone copy may go.
    emitter.emit(
        STAGE_CLEANUP,
        STATUS_START,
        tombstone_path=completion.tombstone_path,
    )
    try:
        cleanup_errors = remove_tombstone(
            completion.tombstone_path, job_id
        )
    except (UnsafeCleanupError, OSError) as exc:
        cleanup_errors = [str(exc)]
    _step(completion, "S9", not cleanup_errors)
    if cleanup_errors:
        completion.cleanup_errors.extend(cleanup_errors)
        completion.state = STATE_LINKED_CLEANUP_PENDING
        completion.error_code = ERROR_CLEANUP
        completion.error_message = "tombstone removal failed: %s" % (
            "; ".join(cleanup_errors),
        )
        emitter.emit(
            STAGE_CLEANUP,
            STATUS_FAILED,
            error_code=ERROR_CLEANUP,
            state=STATE_LINKED_CLEANUP_PENDING,
            errors=list(cleanup_errors),
        )
        return completion

    completion.source_removed = True
    completion.state = STATE_DONE
    emitter.emit(STAGE_CLEANUP, STATUS_DONE, state=STATE_DONE)
    return completion


__all__ = ["complete_migration"]
