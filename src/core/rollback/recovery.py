# -*- coding: utf-8 -*-
"""Restart recovery for interrupted rollbacks (AC-ROL-04, architecture 6.4).

Recovery is observation-driven: the filesystem decides what may be done and
the recorded rollback state only removes ambiguity.  It converges on exactly
one of three outcomes:

* ``ROLLED_BACK`` -- a complete copy sits at the source path and the recorded
  target was removed (only with this job's ownership marker).
* ``ROLLBACK_FAILED`` with ``relinked=True`` -- the junction is back in place;
  the item stays migrated and the target stays untouched.
* ``ROLLBACK_RECOVERY_REQUIRED`` -- the evidence is contradictory, so nothing
  is deleted and the journal keeps the job pending for a human.

Every promotion and deletion uses the exact job-scoped paths recorded in the
journal; a restore slot is only removed when its name matches
``.dw-restore-<job8>`` and it lives in the source parent directory.

Python 3.8 compatible (decision 0005).
"""

import os
from dataclasses import dataclass
from typing import Dict, List, Optional

from ...platform import link_target_matches
from ..migrate import link as link_step
from ..migrate import marker, paths
from ..migrate.cleanup import UnsafeCleanupError, remove_owned_tree, remove_tree
from ..migrate.models import STATUS_DONE, STATUS_FAILED, STATUS_START
from ..migrate.ops import MigrationOps
from ..migrate.progress import Emitter
from ..rules import item_id_for
from ..store.atomic import StoreError
from ..store.history import RESULT_ROLLED_BACK, HistoryStore
from ..store.journal import (
    STATE_ROLLBACK_CONFIG_RESTORED,
    STATE_ROLLBACK_COPIED,
    STATE_ROLLBACK_COPYING,
    STATE_ROLLBACK_FAILED,
    STATE_ROLLBACK_LINK_REMOVED,
    STATE_ROLLBACK_PLANNED,
    STATE_ROLLBACK_RECOVERY_REQUIRED,
    STATE_ROLLBACK_SOURCE_READY,
    STATE_ROLLBACK_VERIFIED,
    STATE_ROLLED_BACK,
    STATE_STATE_ABNORMAL,
    TERMINAL_STATES,
    TYPE_ROLLBACK,
    JournalStore,
)
from ..store.learned import LearnedRulesStore
from .config import restore_configs
from .models import (
    CONFIG_KEEP,
    ERROR_RECOVERY,
    ERROR_RESTORE,
    ERROR_TARGET_DELETE,
    SOURCE_KIND_DIR,
    SOURCE_KIND_FILE,
    SOURCE_KIND_JUNCTION,
    SOURCE_KIND_LINK,
    SOURCE_KIND_MISSING,
    SOURCE_KIND_OTHER,
    STAGE_RECOVER,
    RollbackResult,
)

ROLLBACK_RULE_ID = "rollback"
MAX_DETAIL_ITEMS = 20

# The restore slot was verified (and its owner marker stripped) but the
# same-volume rename into the source path may not have happened yet.
_PROMOTABLE_STATES = (STATE_ROLLBACK_VERIFIED,)
# A plain source directory in these states proves the rename completed.
_SOURCE_RESTORED_STATES = (
    STATE_ROLLBACK_VERIFIED,
    STATE_ROLLBACK_SOURCE_READY,
    STATE_ROLLBACK_CONFIG_RESTORED,
)
# Copy states that cannot justify promoting or deleting the restore slot.
_UNVERIFIED_STATES = (
    STATE_ROLLBACK_PLANNED,
    STATE_ROLLBACK_LINK_REMOVED,
    STATE_ROLLBACK_COPYING,
    STATE_ROLLBACK_COPIED,
)


@dataclass
class RollbackObservation(object):
    """What the filesystem says about the source, target and restore slot."""

    source_kind: str = SOURCE_KIND_MISSING
    source_link_target: str = ""
    source_link_matches: bool = False
    target_kind: str = "missing"  # missing | dir | link | other
    target_owned: bool = False
    restore_kind: str = "missing"  # missing | dir | other
    restore_child_kind: str = "missing"  # missing | dir | other

    def to_dict(self):
        # type: () -> Dict[str, object]
        return {
            "source_kind": self.source_kind,
            "source_link_target": self.source_link_target,
            "source_link_matches": self.source_link_matches,
            "target_kind": self.target_kind,
            "target_owned": self.target_owned,
            "restore_kind": self.restore_kind,
            "restore_child_kind": self.restore_child_kind,
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


def _source_kind(ops, path):
    # type: (object, str) -> str
    if not ops.exists(path):
        return SOURCE_KIND_MISSING
    if ops.is_junction(path):
        return SOURCE_KIND_JUNCTION
    if ops.is_link(path):
        return SOURCE_KIND_LINK
    if ops.is_dir(path):
        return SOURCE_KIND_DIR
    if os.path.isfile(str(path)):
        return SOURCE_KIND_FILE
    return SOURCE_KIND_OTHER


def observe(ops, source_path, final_path, restore_parent, job_id=None):
    # type: (object, str, str, str, str) -> RollbackObservation
    observation = RollbackObservation()
    observation.source_kind = _source_kind(ops, source_path)
    if observation.source_kind == SOURCE_KIND_JUNCTION:
        observation.source_link_target = ops.read_link_target(source_path)
        observation.source_link_matches = link_target_matches(
            observation.source_link_target, final_path
        )
    observation.target_kind = _kind(ops, final_path)
    if observation.target_kind == "dir" and job_id:
        observation.target_owned = marker.owns_directory(final_path, job_id)
    observation.restore_kind = _kind(ops, restore_parent)
    if observation.restore_kind == "dir":
        observation.restore_child_kind = _kind(
            ops, os.path.join(restore_parent, paths.source_basename(source_path))
        )
    return observation


def _step(result, name, ok=True, **fields):
    # type: (RollbackResult, str, bool, object) -> None
    payload = {"stage": STAGE_RECOVER, "step": name, "ok": bool(ok)}
    payload.update(fields)
    result.steps.append(payload)


def _finish(result, emitter, state, code=None, message="", **fields):
    # type: (RollbackResult, Emitter, str, Optional[str], str, object) -> RollbackResult
    result.state = state
    result.error_code = code
    result.error_message = message
    for key, value in fields.items():
        setattr(result, key, value)
    result.recovered = True
    status = (
        STATUS_FAILED
        if state in (STATE_ROLLBACK_RECOVERY_REQUIRED, STATE_STATE_ABNORMAL)
        else STATUS_DONE
    )
    emitter.emit(
        STAGE_RECOVER,
        status,
        state=state,
        error_code=code,
        message=message,
    )
    return result


def _requires_human(result, emitter, message, **fields):
    # type: (RollbackResult, Emitter, str, object) -> RollbackResult
    fields.setdefault("data_kept", True)
    return _finish(
        result,
        emitter,
        STATE_ROLLBACK_RECOVERY_REQUIRED,
        ERROR_RECOVERY,
        message,
        **fields
    )


def _remove_restore_slot(result, restore_parent, source_parent, job_id):
    # type: (RollbackResult, str, str, str) -> None
    """Remove the job's restore slot; never touches anything else."""
    if not restore_parent or not os.path.lexists(restore_parent):
        return
    try:
        errors = remove_tree(
            restore_parent,
            source_parent,
            lambda name: paths.is_restore_dir_name(name, job_id),
        )
    except (UnsafeCleanupError, OSError) as exc:
        errors = [str(exc)]
    for error in errors:
        result.warnings.append("cannot remove restore directory: %s" % error)
    _step(result, "remove-restore-slot", not errors)


def _promote_restore(
    result, emitter, ops, restore_parent, restore_child, source_path, job_id
):
    # type: (RollbackResult, Emitter, object, str, str, str, str) -> bool
    """Rename the verified restore child into the source path."""
    if not paths.is_restore_dir_name(os.path.basename(restore_parent), job_id):
        result.error_message = "restore slot name does not belong to this job"
        return False
    if ops.exists(source_path):
        result.error_message = "the source path is occupied; nothing was moved"
        return False
    if not ops.is_dir(restore_child):
        result.error_message = "the restore copy is not a directory"
        return False
    try:
        same_volume = ops.same_volume(restore_child, source_path)
    except OSError as exc:
        result.error_message = "cannot compare volumes: %s" % exc
        return False
    if not same_volume:
        result.error_message = "the restore copy is not on the source volume"
        return False
    try:
        ops.rename(restore_child, source_path)
    except OSError as exc:
        result.error_message = "cannot move the restore copy into place: %s" % exc
        return False
    result.source_restored = True
    result.link_removed = True
    _step(result, "promote-restore", True)
    emitter.emit(STAGE_RECOVER, STATUS_DONE, step="promote-restore")
    return True


def _restore_configuration(result, backup_dir, tool_id, job_id, state):
    # type: (RollbackResult, str, str, str, str) -> None
    if state == STATE_ROLLBACK_CONFIG_RESTORED:
        return
    try:
        outcome = restore_configs(
            backup_dir,
            choice=CONFIG_KEEP,
            expected_tool_id=tool_id,
            expected_job_id=job_id,
        )
    except (OSError, StoreError, ValueError) as exc:
        result.warnings.append("configuration restore failed: %s" % exc)
        return
    result.config = outcome
    if not outcome.ok:
        result.warnings.append(
            "configuration restore is incomplete: %s" % outcome.reason
        )


def _record_outcome(result, tool_id, source_path, final_path, job_id,
                    history_path, learned_path, size):
    # type: (RollbackResult, str, str, str, str, Optional[str], Optional[str], int) -> None
    if history_path:
        try:
            store = HistoryStore(history_path)
            existing = store.find(job_id, "rollback")
            already = any(
                record.result == RESULT_ROLLED_BACK for record in existing
            )
            if already:
                result.history_recorded = True
            else:
                store.record_rollback(
                    tool=tool_id,
                    item=paths.source_basename(source_path),
                    source=source_path,
                    target=final_path,
                    size=max(0, int(size or 0)),
                    result=RESULT_ROLLED_BACK,
                    job_id=job_id,
                    stage=STAGE_RECOVER,
                    reason="recovered after an interrupted rollback",
                )
                result.history_recorded = True
        except (StoreError, OSError) as exc:
            result.warnings.append("history was not recorded: %s" % exc)

    if learned_path:
        try:
            LearnedRulesStore(learned_path).mark_red(
                item_id_for(tool_id, source_path=source_path),
                reason="rolled_back",
                rule_id=ROLLBACK_RULE_ID,
            )
            result.learned_updated = True
        except (StoreError, OSError, ValueError) as exc:
            result.warnings.append("learned rule was not updated: %s" % exc)


def _target_bytes(final_path):
    # type: (str) -> int
    try:
        from ..migrate.copy import sample_tree

        return sample_tree(final_path)[0]
    except (OSError, ImportError):
        return 0


def _complete_rollback(
    result,
    emitter,
    ops,
    observation,
    source_path,
    final_path,
    job_id,
    source_parent,
    restore_parent,
    restore_child,
    backup_dir,
    state,
    history_path,
    learned_path,
    promote,
):
    # type: (RollbackResult, Emitter, object, RollbackObservation, str, str, str, str, str, str, str, str, Optional[str], Optional[str], bool) -> RollbackResult
    """Finish a data-complete rollback using only proven paths."""
    if promote:
        if not _promote_restore(
            result, emitter, ops, restore_parent, restore_child, source_path, job_id
        ):
            return _requires_human(
                result,
                emitter,
                result.error_message,
                error_code=ERROR_RESTORE,
            )

    size = _target_bytes(final_path) if observation.target_kind == "dir" else 0
    if observation.target_kind == "dir":
        try:
            errors = remove_owned_tree(final_path, os.path.dirname(final_path), job_id)
        except (UnsafeCleanupError, OSError) as exc:
            errors = [str(exc)]
        if errors:
            result.residual_copy = True
            result.error_code = ERROR_TARGET_DELETE
            result.error_message = "; ".join(errors[:MAX_DETAIL_ITEMS])
            result.warnings.extend(errors)
        else:
            result.target_removed = True
        _step(result, "remove-target", not errors)
    elif observation.target_kind != "missing":
        return _requires_human(
            result,
            emitter,
            "the target is not a plain directory; nothing was deleted",
        )

    _restore_configuration(result, backup_dir, result.tool_id, result.job_id, state)
    _remove_restore_slot(result, restore_parent, source_parent, job_id)
    _record_outcome(
        result,
        result.tool_id,
        source_path,
        final_path,
        result.job_id,
        history_path,
        learned_path,
        size,
    )

    result.source_restored = True
    result.data_kept = True
    if result.error_code == ERROR_TARGET_DELETE:
        return _finish(
            result,
            emitter,
            STATE_ROLLED_BACK,
            ERROR_TARGET_DELETE,
            "data was restored; a residual target copy could not be removed",
            residual_copy=True,
        )
    return _finish(
        result,
        emitter,
        STATE_ROLLED_BACK,
        message="rollback recovered and the target was removed",
        target_removed=result.target_removed,
    )


def _recover_source_dir(
    result,
    emitter,
    ops,
    observation,
    recorded_state,
    source_path,
    final_path,
    job_id,
    source_parent,
    restore_parent,
    restore_child,
    backup_dir,
    history_path,
    learned_path,
):
    # type: (...) -> RollbackResult
    """The source path is already a real directory."""
    if observation.target_kind == "missing":
        _remove_restore_slot(result, restore_parent, source_parent, job_id)
        return _complete_rollback(
            result,
            emitter,
            ops,
            observation,
            source_path,
            final_path,
            job_id,
            source_parent,
            restore_parent,
            restore_child,
            backup_dir,
            recorded_state,
            history_path,
            learned_path,
            promote=False,
        )
    if observation.target_kind != "dir":
        return _requires_human(
            result,
            emitter,
            "the source is a directory but the target is not",
        )
    if not observation.target_owned:
        return _requires_human(
            result,
            emitter,
            "the source is a directory and the target has no ownership marker",
        )
    if recorded_state in _SOURCE_RESTORED_STATES:
        return _complete_rollback(
            result,
            emitter,
            ops,
            observation,
            source_path,
            final_path,
            job_id,
            source_parent,
            restore_parent,
            restore_child,
            backup_dir,
            recorded_state,
            history_path,
            learned_path,
            promote=False,
        )
    if recorded_state in _UNVERIFIED_STATES or recorded_state is None:
        return _requires_human(
            result,
            emitter,
            "a plain source directory and an owned target cannot be "
            "disambiguated from state %s; no target was deleted"
            % (recorded_state,),
        )
    return _requires_human(
        result,
        emitter,
        "unexpected state %s with a plain source directory" % (recorded_state,),
    )


def _recover_source_junction(
    result,
    emitter,
    ops,
    observation,
    source_path,
    final_path,
    job_id,
    source_parent,
    restore_parent,
):
    # type: (...) -> RollbackResult
    """A junction still points at the target: the item stays migrated."""
    if observation.target_kind != "dir" or not observation.target_owned:
        return _requires_human(
            result,
            emitter,
            "the source junction exists but the target is missing or unowned; "
            "the junction was left in place",
        )
    if not observation.source_link_matches:
        return _requires_human(
            result,
            emitter,
            "the source junction points to %r, not %r"
            % (observation.source_link_target, final_path),
        )
    _remove_restore_slot(result, restore_parent, source_parent, job_id)
    return _finish(
        result,
        emitter,
        STATE_ROLLBACK_FAILED,
        message="the junction is intact; the item stays migrated",
        data_kept=True,
        link_removed=False,
    )


def _recover_source_missing(
    result,
    emitter,
    ops,
    observation,
    recorded_state,
    source_path,
    final_path,
    job_id,
    source_parent,
    restore_parent,
    restore_child,
    backup_dir,
    history_path,
    learned_path,
):
    # type: (...) -> RollbackResult
    """The source path is empty because the junction was removed."""
    if observation.target_kind == "dir" and not observation.target_owned:
        return _requires_human(
            result,
            emitter,
            "the source is missing and the target has no ownership marker",
        )

    promotable = (
        recorded_state in _PROMOTABLE_STATES
        and observation.restore_kind == "dir"
        and observation.restore_child_kind == "dir"
    )
    if observation.target_kind == "dir" and promotable:
        return _complete_rollback(
            result,
            emitter,
            ops,
            observation,
            source_path,
            final_path,
            job_id,
            source_parent,
            restore_parent,
            restore_child,
            backup_dir,
            recorded_state,
            history_path,
            learned_path,
            promote=True,
        )

    if observation.target_kind == "dir":
        # Re-link the source to the intact target: the rollback is abandoned
        # and the item stays migrated with at least one complete copy.
        try:
            link_step.ensure_link(ops, source_path, final_path)
            check = link_step.verify_link(ops, source_path, final_path)
        except (link_step.LinkStepError, OSError) as exc:
            return _requires_human(
                result,
                emitter,
                "cannot restore the junction: %s" % exc,
            )
        if not check.ok:
            return _requires_human(
                result,
                emitter,
                "the restored junction could not be verified: %s" % check.reason,
            )
        _remove_restore_slot(result, restore_parent, source_parent, job_id)
        return _finish(
            result,
            emitter,
            STATE_ROLLBACK_FAILED,
            message="the junction was restored; the item stays migrated",
            data_kept=True,
            relinked=True,
            link_removed=False,
        )

    if observation.target_kind == "missing" and promotable:
        return _complete_rollback(
            result,
            emitter,
            ops,
            observation,
            source_path,
            final_path,
            job_id,
            source_parent,
            restore_parent,
            restore_child,
            backup_dir,
            recorded_state,
            history_path,
            learned_path,
            promote=True,
        )

    if observation.target_kind != "missing":
        return _requires_human(
            result,
            emitter,
            "the target is not a plain directory; nothing was touched",
        )
    return _requires_human(
        result,
        emitter,
        "the source, the target and a verified restore copy are not all "
        "present; no data was deleted",
    )


def _decide(
    result,
    emitter,
    ops,
    observation,
    recorded_state,
    source_path,
    final_path,
    job_id,
    source_parent,
    restore_parent,
    restore_child,
    backup_dir,
    history_path,
    learned_path,
):
    # type: (...) -> RollbackResult
    kind = observation.source_kind
    if kind == SOURCE_KIND_DIR:
        return _recover_source_dir(
            result,
            emitter,
            ops,
            observation,
            recorded_state,
            source_path,
            final_path,
            job_id,
            source_parent,
            restore_parent,
            restore_child,
            backup_dir,
            history_path,
            learned_path,
        )
    if kind == SOURCE_KIND_JUNCTION:
        return _recover_source_junction(
            result,
            emitter,
            ops,
            observation,
            source_path,
            final_path,
            job_id,
            source_parent,
            restore_parent,
        )
    if kind == SOURCE_KIND_MISSING:
        return _recover_source_missing(
            result,
            emitter,
            ops,
            observation,
            recorded_state,
            source_path,
            final_path,
            job_id,
            source_parent,
            restore_parent,
            restore_child,
            backup_dir,
            history_path,
            learned_path,
        )
    if kind == SOURCE_KIND_LINK:
        return _requires_human(
            result,
            emitter,
            "the source is a non-junction link; it will not be removed",
        )
    return _requires_human(
        result,
        emitter,
        "the source is neither a directory nor a junction; nothing was touched",
    )


def _valid_restore_dir(restore_parent, source_parent, job_id):
    # type: (str, str, str) -> bool
    if not restore_parent:
        return False
    if os.path.normcase(os.path.dirname(restore_parent)) != os.path.normcase(
        source_parent
    ):
        return False
    return paths.is_restore_dir_name(os.path.basename(restore_parent), job_id)


def recover_rollback(
    job_id,
    tool_id,
    source_path,
    final_path,
    backup_dir="",
    restore_dir=None,
    recorded_state=None,
    jobs_dir=None,
    ops=None,
    progress_cb=None,
    history_path=None,
    learned_path=None,
):
    # type: (str, str, str, str, str, Optional[str], Optional[str], Optional[str], object, object, Optional[str], Optional[str]) -> RollbackResult
    """Recover one interrupted rollback from the journal and the filesystem.

    ``jobs_dir`` loads the rollback journal (and fills ``recorded_state``,
    ``restore_dir`` and ``backup_dir``); ``recorded_state`` may also be passed
    directly for observation-only recovery.
    """
    ops = ops or MigrationOps()
    # Marker ownership and ``.dw-restore-<job8>`` names both compare against
    # the full lowercased id, never a bare 8-char prefix.
    job_id = (job_id or "").lower()
    result = RollbackResult(
        job_id=job_id,
        tool_id=tool_id or "",
        source_path=source_path or "",
        final_path=final_path or "",
    )
    emitter = Emitter(progress_cb, result.job_id, result.tool_id)

    try:
        paths.job_prefix(job_id)
        paths.validate_tool_id(tool_id)
        source_path = os.path.abspath(os.path.normpath(source_path))
        final_path = os.path.abspath(os.path.normpath(final_path))
    except (paths.PathValidationError, ValueError, TypeError) as exc:
        return _finish(
            result,
            emitter,
            STATE_ROLLBACK_RECOVERY_REQUIRED,
            ERROR_RECOVERY,
            str(exc),
            data_kept=True,
        )

    source_parent = os.path.dirname(source_path)
    restore_parent = (
        os.path.abspath(os.path.normpath(restore_dir))
        if restore_dir
        else paths.restore_path(source_path, job_id)
    )
    restore_child = os.path.join(restore_parent, paths.source_basename(source_path))

    entry = None
    journal_store = None
    if jobs_dir:
        journal_store = JournalStore(jobs_dir)
        entry = journal_store.load(job_id, TYPE_ROLLBACK)
        for warning in journal_store.warnings:
            result.warnings.append(str(warning.get("message", "")))
        if entry is None and recorded_state is None:
            return _finish(
                result,
                emitter,
                STATE_ROLLBACK_RECOVERY_REQUIRED,
                ERROR_RECOVERY,
                "no rollback journal exists for this job",
                data_kept=True,
            )
        if entry is not None:
            if entry.tool_id and entry.tool_id != tool_id:
                return _finish(
                    result,
                    emitter,
                    STATE_ROLLBACK_RECOVERY_REQUIRED,
                    ERROR_RECOVERY,
                    "journal tool id does not match",
                    data_kept=True,
                )
            if entry.source and not paths.same_path(entry.source, source_path):
                return _finish(
                    result,
                    emitter,
                    STATE_ROLLBACK_RECOVERY_REQUIRED,
                    ERROR_RECOVERY,
                    "journal source does not match",
                    data_kept=True,
                )
            if entry.final and not paths.same_path(entry.final, final_path):
                return _finish(
                    result,
                    emitter,
                    STATE_ROLLBACK_RECOVERY_REQUIRED,
                    ERROR_RECOVERY,
                    "journal target does not match",
                    data_kept=True,
                )
            if restore_dir is None and entry.restore_dir:
                restore_parent = os.path.abspath(
                    os.path.normpath(entry.restore_dir)
                )
                restore_child = os.path.join(
                    restore_parent, paths.source_basename(source_path)
                )
            if not backup_dir:
                backup_dir = entry.backup_dir or ""
            if recorded_state is None:
                recorded_state = entry.state

    result.restore_dir = restore_parent
    result.backup_dir = backup_dir

    if not _valid_restore_dir(restore_parent, source_parent, job_id):
        return _finish(
            result,
            emitter,
            STATE_ROLLBACK_RECOVERY_REQUIRED,
            ERROR_RECOVERY,
            "the recorded restore directory does not belong to this job",
            data_kept=True,
        )

    emitter.emit(
        STAGE_RECOVER,
        STATUS_START,
        recorded_state=recorded_state,
        source_path=source_path,
        final_path=final_path,
    )

    if recorded_state in TERMINAL_STATES:
        result.data_kept = True
        return _finish(
            result,
            emitter,
            recorded_state,
            message="the rollback journal is already terminal",
        )

    try:
        observation = observe(
            ops, source_path, final_path, restore_parent, job_id=job_id
        )
    except OSError as exc:
        return _finish(
            result,
            emitter,
            STATE_ROLLBACK_RECOVERY_REQUIRED,
            ERROR_RECOVERY,
            "cannot inspect the job's paths: %s" % exc,
            data_kept=True,
        )
    _step(result, "R0-observe", True, observed=observation.to_dict())

    if observation.restore_kind not in ("missing", "dir"):
        return _finish(
            result,
            emitter,
            STATE_ROLLBACK_RECOVERY_REQUIRED,
            ERROR_RECOVERY,
            "the restore slot is not a plain directory",
            data_kept=True,
        )

    state = recorded_state
    result = _decide(
        result,
        emitter,
        ops,
        observation,
        state,
        source_path,
        final_path,
        job_id,
        source_parent,
        restore_parent,
        restore_child,
        backup_dir,
        history_path,
        learned_path,
    )

    if journal_store is not None and entry is not None:
        payload = {
            "state": result.state,
            "recovered": True,
            "message": result.error_message,
            "source_restored": result.source_restored,
            "target_removed": result.target_removed,
            "relinked": result.relinked,
        }
        try:
            journal_store.mark_recovered(
                job_id,
                result.state,
                payload=payload,
                entry_type=TYPE_ROLLBACK,
            )
        except (StoreError, OSError) as exc:
            result.warnings.append("journal recovery update failed: %s" % exc)
    return result


__all__ = ["RollbackObservation", "observe", "recover_rollback"]
