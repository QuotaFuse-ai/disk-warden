# -*- coding: utf-8 -*-
"""Startup/recovery matrix for the swap half (architecture 6.4).

Recovery is observation-driven: the filesystem decides what may be done, and
the recorded journal state only removes ambiguity.  Every action is a rename,
an unlink or a deletion of a path whose name or ownership marker proves it
belongs to this job.  When the evidence is not conclusive, nothing is touched
and the job lands in ``RECOVERY_REQUIRED`` (architecture 6.4 principle).

Re-running recovery on the same tree is safe: a restored source, an absent
link and an absent target are all treated as already-done (architecture 6.5).

Python 3.8 compatible (decision 0005).
"""

import os
from dataclasses import dataclass
from typing import Dict, Optional

from ...platform import link_target_matches
from . import link, marker, paths, swap
from .cleanup import UnsafeCleanupError, remove_tombstone
from .models import (
    ERROR_RECOVERY,
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
    STATE_BACKED_UP,
    STATE_COPIED,
    STATE_COPYING,
    STAGE_RECOVER,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_START,
    CompletionResult,
)
from .ops import MigrationOps
from .progress import Emitter

# States observed before the source was renamed.  The target may be absent
# (S1-S4) or present and owned by this job (S5).
_PRE_SWAP_STATES = (
    STATE_PLANNED,
    STATE_BACKED_UP,
    STATE_COPYING,
    STATE_COPIED,
)
_PRE_SOURCE_SWAP_STATES = _PRE_SWAP_STATES + (STATE_COPY_VERIFIED,)

# States that promise a verified link at the source path.  Seeing a plain
# source directory in one of them is an intent/observation mismatch.
_LINKED_STATES = (
    STATE_VERIFIED_LINK,
    STATE_DONE,
    STATE_LINKED_CLEANUP_PENDING,
)


@dataclass
class Observation(object):
    """What the filesystem says about the five paths of a job."""

    source_kind: str = "missing"  # missing | link | dir | other
    source_link_target: Optional[str] = None
    source_link_matches: bool = False
    tombstone_kind: str = "missing"  # missing | link | dir | other
    final_kind: str = "missing"  # missing | link | dir | other
    final_owned: bool = False
    staging_exists: bool = False

    def to_dict(self):
        # type: () -> Dict[str, object]
        return {
            "source_kind": self.source_kind,
            "source_link_target": self.source_link_target,
            "source_link_matches": self.source_link_matches,
            "tombstone_kind": self.tombstone_kind,
            "final_kind": self.final_kind,
            "final_owned": self.final_owned,
            "staging_exists": self.staging_exists,
        }


def _kind(ops, path):
    # type: (object, str) -> str
    if not ops.exists(path):
        return "missing"
    if ops.is_link(path):
        return "link"
    if ops.is_dir(path):
        return "dir"
    return "other"


def observe(ops, source_path, tombstone_path, final_path, staging_path=None,
            job_id=None):
    # type: (object, str, str, str, str, str) -> Observation
    observation = Observation()
    observation.source_kind = _kind(ops, source_path)
    if observation.source_kind == "link":
        observation.source_link_target = ops.read_link_target(source_path)
        observation.source_link_matches = link_target_matches(
            observation.source_link_target, final_path
        )
    observation.tombstone_kind = _kind(ops, tombstone_path)
    observation.final_kind = _kind(ops, final_path)
    if observation.final_kind == "dir" and job_id:
        observation.final_owned = marker.owns_directory(final_path, job_id)
    observation.staging_exists = bool(
        staging_path and ops.exists(staging_path)
    )
    return observation


def _default_staging_path(source_path, final_path, job_id):
    # type: (str, str, str) -> str
    return os.path.join(
        os.path.dirname(os.path.abspath(final_path)),
        ".dw-partial-%s" % paths.job_prefix(job_id),
        paths.source_basename(source_path),
    )


def _step(result, name, ok, **fields):
    # type: (CompletionResult, str, bool, object) -> None
    payload = {"step": name, "ok": bool(ok)}
    payload.update(fields)
    result.steps.append(payload)


def _finish(result, emitter, state, code=None, message="", observed=None,
            **fields):
    # type: (CompletionResult, Emitter, str, str, str, object, object) -> CompletionResult
    result.state = state
    result.error_code = code
    result.error_message = message
    for key, value in fields.items():
        setattr(result, key, value)
    status = STATUS_FAILED if state == STATE_RECOVERY_REQUIRED else STATUS_DONE
    emitter.emit(
        STAGE_RECOVER,
        status,
        state=state,
        error_code=code,
        message=message,
        observed=observed,
    )
    return result


def _recover_link_source(result, emitter, ops, observation,
                         source_path, tombstone_path, final_path, job_id,
                         staging_path):
    """The source path is a link: this is the S7/S8 part of the matrix."""
    if observation.final_kind != "dir" or not observation.final_owned:
        return _finish(
            result,
            emitter,
            STATE_RECOVERY_REQUIRED,
            ERROR_RECOVERY,
            "source is a link but the target is missing or not owned by job",
        )
    if not observation.source_link_matches:
        return _finish(
            result,
            emitter,
            STATE_RECOVERY_REQUIRED,
            ERROR_RECOVERY,
            "source junction points to %r, not %r"
            % (observation.source_link_target, final_path),
        )

    check = link.verify_link(ops, source_path, final_path)
    result.link_created = True
    result.link_target = check.target
    result.link_verified = check.ok
    result.source_removed = True
    _step(result, "S8", check.ok, target=check.target, reason=check.reason)

    if not check.ok:
        if observation.tombstone_kind != "dir":
            # Without the tombstone the link is the only data at the source
            # path; removing it would leave the user with nothing there.
            return _finish(
                result,
                emitter,
                STATE_RECOVERY_REQUIRED,
                ERROR_RECOVERY,
                "link verification failed and the tombstone is missing: %s"
                % check.reason,
            )
        try:
            link.remove_own_link(ops, source_path, final_path)
            _step(result, "S8-remove-link", True)
        except link.LinkStepError as exc:
            return _finish(
                result,
                emitter,
                STATE_RECOVERY_REQUIRED,
                ERROR_RECOVERY,
                "cannot remove the job's junction: %s" % exc,
            )
        try:
            swap.restore_source(ops, source_path, tombstone_path, job_id)
            _step(result, "S8-restore-source", True)
        except swap.SwapError as exc:
            return _finish(
                result,
                emitter,
                STATE_RECOVERY_REQUIRED,
                ERROR_RECOVERY,
                "link removed but the source cannot be restored: %s" % exc,
            )
        _discard_staging(result, staging_path, job_id)
        return _finish(
            result,
            emitter,
            STATE_ROLLED_BACK,
            rolled_back=True,
            source_removed=False,
            link_created=False,
            link_verified=False,
            message="link verification failed; source restored",
        )

    # The staging leaf was renamed to the final path before the crash; only
    # the job-prefixed parent (and the copy log) can be left behind.
    _discard_staging(result, staging_path, job_id)
    if observation.tombstone_kind == "dir":
        errors = _cleanup_tombstone(result, tombstone_path, job_id)
        if errors:
            return _finish(
                result,
                emitter,
                STATE_LINKED_CLEANUP_PENDING,
                cleanup_errors=list(result.cleanup_errors),
                message="link is valid; tombstone removal will be retried",
            )
    return _finish(
        result,
        emitter,
        STATE_DONE,
        source_removed=True,
        message="link is valid",
    )


def _recover_source_present(result, emitter, ops, observation, recorded_state,
                            source_path, tombstone_path, final_path, job_id,
                            staging_path):
    """The source path is a real directory: S5/S6 were never completed."""
    if observation.tombstone_kind == "dir":
        return _finish(
            result,
            emitter,
            STATE_RECOVERY_REQUIRED,
            ERROR_RECOVERY,
            "source and tombstone both exist; refusing to guess",
        )
    if observation.final_kind == "link" or observation.final_kind == "other":
        return _finish(
            result,
            emitter,
            STATE_RECOVERY_REQUIRED,
            ERROR_RECOVERY,
            "final target is not a plain directory",
        )
    if recorded_state in _LINKED_STATES:
        return _finish(
            result,
            emitter,
            STATE_RECOVERY_REQUIRED,
            ERROR_RECOVERY,
            "journal state %s expects a link at the source path"
            % recorded_state,
        )

    if observation.final_kind == "dir":
        if not observation.final_owned:
            return _finish(
                result,
                emitter,
                STATE_RECOVERY_REQUIRED,
                ERROR_RECOVERY,
                "final target exists without this job's ownership marker",
            )
        if recorded_state == STATE_TARGET_READY or recorded_state in _PRE_SOURCE_SWAP_STATES:
            errors = _discard_target(result, final_path, job_id)
            _discard_staging(result, staging_path, job_id)
            return _finish(
                result,
                emitter,
                STATE_FAILED,
                ERROR_RECOVERY if errors else None,
                "promoted target removed; source was never moved",
                cleanup_errors=list(result.cleanup_errors),
            )
        if recorded_state in (STATE_SOURCE_RENAMED, STATE_LINKED, STATE_ROLLED_BACK):
            # A previous rollback stopped after restoring the source.  S8
            # rollback keeps the target on purpose; SOURCE_RENAMED does not.
            if recorded_state == STATE_SOURCE_RENAMED:
                _discard_target(result, final_path, job_id)
            return _finish(
                result,
                emitter,
                STATE_ROLLED_BACK,
                rolled_back=True,
                message="source already restored",
            )
        return _finish(
            result,
            emitter,
            STATE_RECOVERY_REQUIRED,
            ERROR_RECOVERY,
            "source is intact and an owned target exists; "
            "recorded state does not disambiguate rollback",
        )

    _discard_staging(result, staging_path, job_id)
    if recorded_state in _PRE_SOURCE_SWAP_STATES:
        return _finish(
            result,
            emitter,
            STATE_FAILED,
            message="staging removed; source was never moved",
        )
    if recorded_state == STATE_TARGET_READY:
        return _finish(
            result,
            emitter,
            STATE_RECOVERY_REQUIRED,
            ERROR_RECOVERY,
            "journal says TARGET_READY but the target is missing",
        )
    return _finish(
        result,
        emitter,
        STATE_ROLLED_BACK,
        rolled_back=True,
        message="source is already in its pre-migration place",
    )


def _recover_tombstone_only(result, emitter, ops, observation, recorded_state,
                            source_path, tombstone_path, final_path, job_id,
                            staging_path):
    """The source was renamed but no link exists at its path."""
    if recorded_state in _LINKED_STATES:
        return _finish(
            result,
            emitter,
            STATE_RECOVERY_REQUIRED,
            ERROR_RECOVERY,
            "journal state %s expects a link, but only the tombstone exists"
            % recorded_state,
        )
    if observation.final_kind == "link" or observation.final_kind == "other":
        return _finish(
            result,
            emitter,
            STATE_RECOVERY_REQUIRED,
            ERROR_RECOVERY,
            "final target is not a plain directory",
        )

    try:
        swap.restore_source(ops, source_path, tombstone_path, job_id)
        _step(result, "rollback-restore-source", True)
    except swap.SwapError as exc:
        return _finish(
            result,
            emitter,
            STATE_RECOVERY_REQUIRED,
            ERROR_RECOVERY,
            "cannot restore the source from the tombstone: %s" % exc,
        )

    if observation.final_kind == "dir":
        if not observation.final_owned:
            return _finish(
                result,
                emitter,
                STATE_RECOVERY_REQUIRED,
                ERROR_RECOVERY,
                "source restored, but the target has no ownership marker",
            )
        errors = _discard_target(result, final_path, job_id)
        if errors:
            return _finish(
                result,
                emitter,
                STATE_RECOVERY_REQUIRED,
                ERROR_RECOVERY,
                "source restored, but the target could not be removed",
                cleanup_errors=list(result.cleanup_errors),
            )
    _discard_staging(result, staging_path, job_id)
    return _finish(
        result,
        emitter,
        STATE_ROLLED_BACK,
        rolled_back=True,
        link_created=False,
        link_verified=False,
        source_removed=False,
        message="source restored and target removed",
    )


def _discard_staging(result, staging_path, job_id):
    # type: (CompletionResult, str, str) -> None
    if not staging_path:
        return
    try:
        errors = swap.discard_staging(staging_path, job_id)
    except (UnsafeCleanupError, OSError) as exc:
        errors = [str(exc)]
    if errors:
        result.cleanup_errors.extend(errors)
    _step(result, "discard-staging", not errors)


def _discard_target(result, final_path, job_id):
    # type: (CompletionResult, str, str) -> list
    try:
        errors = swap.discard_target(final_path, job_id)
    except (UnsafeCleanupError, OSError) as exc:
        errors = [str(exc)]
    if errors:
        result.cleanup_errors.extend(errors)
    _step(result, "discard-target", not errors)
    return errors


def _cleanup_tombstone(result, tombstone_path, job_id):
    # type: (CompletionResult, str, str) -> list
    try:
        errors = remove_tombstone(tombstone_path, job_id)
    except (UnsafeCleanupError, OSError) as exc:
        errors = [str(exc)]
    if errors:
        result.cleanup_errors.extend(errors)
    _step(result, "S9", not errors)
    return errors


def recover_job(
    job_id,
    tool_id,
    source_path,
    final_path,
    staging_path=None,
    recorded_state=None,
    ops=None,
    progress_cb=None,
):
    # type: (str, str, str, str, str, str, object, object) -> CompletionResult
    """Bring a job to an explicit terminal state using only safe evidence.

    ``recorded_state`` is the last journal state (architecture 6.4); passing
    ``None`` makes recovery rely on the observation alone and report
    ``RECOVERY_REQUIRED`` whenever two interpretations are possible.
    """
    ops = ops or MigrationOps()
    source_path = os.path.abspath(os.path.normpath(source_path))
    final_path = os.path.abspath(os.path.normpath(final_path))
    try:
        tombstone_path = paths.tombstone_path(source_path, job_id)
        if staging_path:
            staging_path = os.path.abspath(os.path.normpath(staging_path))
        else:
            staging_path = _default_staging_path(
                source_path, final_path, job_id
            )
    except (paths.PathValidationError, ValueError) as exc:
        result = CompletionResult(
            job_id=job_id or "",
            tool_id=tool_id,
            source_path=source_path,
            final_path=final_path,
            staging_path=staging_path or "",
            tombstone_path="",
        )
        emitter = Emitter(progress_cb, job_id, tool_id)
        return _finish(
            result, emitter, STATE_RECOVERY_REQUIRED, ERROR_RECOVERY, str(exc)
        )

    result = CompletionResult(
        job_id=job_id,
        tool_id=tool_id,
        source_path=source_path,
        final_path=final_path,
        staging_path=staging_path,
        tombstone_path=tombstone_path,
    )
    emitter = Emitter(progress_cb, job_id, tool_id)
    emitter.emit(
        STAGE_RECOVER,
        STATUS_START,
        recorded_state=recorded_state,
        source_path=source_path,
        final_path=final_path,
    )

    try:
        observation = observe(
            ops, source_path, tombstone_path, final_path, staging_path,
            job_id=job_id,
        )
    except OSError as exc:
        return _finish(
            result,
            emitter,
            STATE_RECOVERY_REQUIRED,
            ERROR_RECOVERY,
            "cannot inspect the job's paths: %s" % exc,
        )

    _step(result, "S0-observe", True, observed=observation.to_dict())

    if observation.tombstone_kind not in ("missing", "dir"):
        return _finish(
            result,
            emitter,
            STATE_RECOVERY_REQUIRED,
            ERROR_RECOVERY,
            "tombstone is not a plain directory",
            observed=observation.to_dict(),
        )
    if observation.source_kind == "other":
        return _finish(
            result,
            emitter,
            STATE_RECOVERY_REQUIRED,
            ERROR_RECOVERY,
            "source path is neither a directory nor a junction",
            observed=observation.to_dict(),
        )

    if observation.source_kind == "link":
        return _recover_link_source(
            result, emitter, ops, observation,
            source_path, tombstone_path, final_path, job_id, staging_path,
        )
    if observation.source_kind == "dir":
        return _recover_source_present(
            result, emitter, ops, observation, recorded_state,
            source_path, tombstone_path, final_path, job_id, staging_path,
        )
    if observation.tombstone_kind == "dir":
        return _recover_tombstone_only(
            result, emitter, ops, observation, recorded_state,
            source_path, tombstone_path, final_path, job_id, staging_path,
        )
    return _finish(
        result,
        emitter,
        STATE_RECOVERY_REQUIRED,
        ERROR_RECOVERY,
        "source, tombstone and link are all absent; data may exist only at %s"
        % final_path,
        observed=observation.to_dict(),
    )


__all__ = ["Observation", "observe", "recover_job"]
