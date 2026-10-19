# -*- coding: utf-8 -*-
"""Migration engine A: prepare -> backup -> copy -> verify (S1-S4).

This module never renames, deletes or links the source.  It produces a fully
verified staging copy plus a config backup; T10 consumes ``MigrationResult``
to swap the source with a tombstone, promote staging and create the junction.

Operational failures (bad input, backup error, robocopy >= 8, verification
mismatch) are returned as ``MigrationResult(ok=False)`` after removing this
job's temporary directories.  Cancellation raises ``MigrationCancelled``
because a cancelled job has no result.

Python 3.8 compatible (decision 0005).
"""

import os
import time
from dataclasses import dataclass
from typing import Dict, Optional

from ...platform import TransferError, is_reparse_stat
from ..scan.cancel import is_cancelled
from . import paths
from .backup import BackupError, backup_config
from .cleanup import UnsafeCleanupError, remove_tree
from .copy import LOG_NAME, copy_to_staging
from .models import (
    ERROR_BACKUP,
    ERROR_COPY,
    ERROR_PREPARE,
    ERROR_VERIFY,
    STAGE_BACKUP,
    STAGE_PREPARE,
    STAGE_VERIFY,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_START,
    MigrationCancelled,
    MigrationRequest,
    MigrationResult,
)
from .verify import verify_trees


@dataclass
class _Prepared(object):
    job_id: str
    source_path: str
    config_root: str
    target_root: str
    backup_root: str
    tool_dir: str
    final_path: str
    staging_parent: str
    staging_path: str


class _Emitter(object):
    def __init__(self, callback):
        # type: (object) -> None
        self._callback = callback
        self._started = time.monotonic()

    def emit(self, payload):
        # type: (Dict[str, object]) -> None
        if self._callback is None:
            return
        payload = dict(payload)
        payload["elapsed_seconds"] = round(
            time.monotonic() - self._started, 3
        )
        self._callback(payload)


def _check_cancelled(token):
    # type: (object) -> None
    if is_cancelled(token):
        raise MigrationCancelled("migration cancelled")


def _cleanup_staging(prepared, job_id):
    # type: (_Prepared, str) -> tuple
    try:
        errors = remove_tree(
            prepared.staging_parent,
            os.path.dirname(prepared.staging_parent),
            lambda name: paths.is_partial_dir_name(name, job_id),
        )
        return not errors, "; ".join(errors)
    except (UnsafeCleanupError, OSError) as exc:
        return False, str(exc)


def _cleanup_backup(backup_root, job_id):
    # type: (str, str) -> tuple
    """Remove only a backup directory whose name carries this job's prefix."""
    try:
        with os.scandir(backup_root) as entries:
            candidates = [
                entry.path
                for entry in entries
                if paths.is_backup_dir_name(entry.name, job_id)
            ]
    except OSError:
        return True, ""
    removed_all = True
    details = []
    for candidate in candidates:
        try:
            errors = remove_tree(
                candidate,
                backup_root,
                lambda name: paths.is_backup_dir_name(name, job_id),
            )
        except (UnsafeCleanupError, OSError) as exc:
            removed_all = False
            details.append(str(exc))
            continue
        if errors:
            removed_all = False
            details.extend(errors)
    return removed_all, "; ".join(details)


def _failure(result, code, message, temp_removed=False):
    # type: (MigrationResult, str, str, bool) -> MigrationResult
    result.ok = False
    result.error_code = code
    result.error_message = message
    result.temp_removed = temp_removed
    return result


def _prepare(request):
    # type: (MigrationRequest) -> tuple
    """Validate inputs and derive paths; returns ``(_Prepared, None)`` or
    ``(None, message)``."""
    job_id = (request.job_id or "").lower() or paths.new_job_id()
    source_path = os.path.abspath(os.path.normpath(request.source_path))
    target_root = os.path.abspath(os.path.normpath(request.target_root))

    try:
        tool_dir = os.path.join(target_root, paths.validate_tool_id(request.tool_id))
        final_path = paths.final_path(target_root, request.tool_id, source_path)
        staging_parent = paths.staging_parent(
            target_root, request.tool_id, job_id
        )
        staging_path = paths.staging_path(
            target_root, request.tool_id, job_id, source_path
        )
    except paths.PathValidationError as exc:
        return None, str(exc)

    try:
        info = os.lstat(source_path)
    except FileNotFoundError:
        return None, "source does not exist: %s" % source_path
    except OSError as exc:
        return None, "cannot inspect source: %s" % exc
    if is_reparse_stat(info):
        return None, "source is already a reparse point: %s" % source_path
    if os.path.isdir(source_path):
        pass
    elif not os.path.isfile(source_path):
        return None, "source is neither a file nor a directory: %s" % source_path

    if not os.path.isdir(target_root):
        return None, "target root does not exist: %s" % target_root
    if paths.is_inside(target_root, source_path):
        return None, "target root is inside the source: %s" % target_root
    if paths.is_inside(source_path, target_root):
        return None, "source is inside the target root: %s" % source_path
    if paths.is_inside(staging_path, source_path) or paths.is_inside(
        final_path, source_path
    ):
        return None, "target path overlaps the source"

    config_root = request.config_root or source_path
    config_root = os.path.abspath(os.path.normpath(config_root))
    try:
        config_info = os.lstat(config_root)
    except OSError as exc:
        return None, "cannot inspect config root %s: %s" % (config_root, exc)
    if is_reparse_stat(config_info):
        return None, "config root is a reparse point: %s" % config_root

    backup_root = request.backup_root
    if backup_root:
        backup_root = os.path.abspath(os.path.normpath(backup_root))
    else:
        backup_root = os.path.join(os.path.dirname(target_root), "backup")

    return (
        _Prepared(
            job_id=job_id,
            source_path=source_path,
            config_root=config_root,
            target_root=target_root,
            backup_root=backup_root,
            tool_dir=tool_dir,
            final_path=final_path,
            staging_parent=staging_parent,
            staging_path=staging_path,
        ),
        None,
    )


def _empty_result(request, prepared):
    # type: (MigrationRequest, Optional[_Prepared]) -> MigrationResult
    return MigrationResult(
        job_id=prepared.job_id if prepared else (request.job_id or ""),
        tool_id=request.tool_id,
        source_path=prepared.source_path if prepared else request.source_path,
        final_path=prepared.final_path if prepared else "",
        staging_path=prepared.staging_path if prepared else "",
        ok=False,
    )


def prepare_copy(
    request,
    transfer=None,
    cancel_token=None,
    progress_cb=None,
    sample_seconds=2.0,
):
    # type: (MigrationRequest, object, object, object, float) -> MigrationResult
    """Back up config, copy to staging and verify, leaving the source untouched.

    ``transfer`` is injectable for tests and fault drills; it defaults to the
    platform ``run_robocopy``.  ``progress_cb`` receives stage events for
    ``prepare``, ``backup``, ``copy`` and ``verify``.

    Raises ``MigrationCancelled`` when ``cancel_token`` fires; the staging
    directory is removed first.
    """
    emitter = _Emitter(progress_cb)
    emitter.emit(
        {
            "stage": STAGE_PREPARE,
            "status": STATUS_START,
            "job_id": request.job_id or "",
            "tool_id": request.tool_id,
            "source_path": request.source_path,
        }
    )

    prepared, error = _prepare(request)
    if prepared is None:
        result = _empty_result(request, None)
        emitter.emit(
            {
                "stage": STAGE_PREPARE,
                "status": STATUS_FAILED,
                "job_id": result.job_id,
                "tool_id": request.tool_id,
                "message": error,
            }
        )
        return _failure(result, ERROR_PREPARE, error)

    result = _empty_result(request, prepared)
    emitter.emit(
        {
            "stage": STAGE_PREPARE,
            "status": STATUS_DONE,
            "job_id": prepared.job_id,
            "tool_id": request.tool_id,
            "source_path": prepared.source_path,
            "final_path": prepared.final_path,
            "staging_path": prepared.staging_path,
        }
    )

    _check_cancelled(cancel_token)

    # S1: config backup before any data movement.
    emitter.emit(
        {
            "stage": STAGE_BACKUP,
            "status": STATUS_START,
            "job_id": prepared.job_id,
            "tool_id": request.tool_id,
            "source_path": prepared.source_path,
            "config_root": prepared.config_root,
            "backup_root": prepared.backup_root,
        }
    )
    try:
        manifest = backup_config(
            request.tool_id,
            prepared.config_root,
            prepared.backup_root,
            prepared.job_id,
            config_files=request.config_files,
        )
    except (BackupError, OSError) as exc:
        _, cleanup_error = _cleanup_backup(
            prepared.backup_root, prepared.job_id
        )
        message = str(exc)
        if cleanup_error:
            message = "%s (backup cleanup failed: %s)" % (message, cleanup_error)
        emitter.emit(
            {
                "stage": STAGE_BACKUP,
                "status": STATUS_FAILED,
                "job_id": prepared.job_id,
                "tool_id": request.tool_id,
                "message": message,
            }
        )
        return _failure(result, ERROR_BACKUP, message, temp_removed=True)

    result.backup = manifest
    result.backup_dir = manifest.backup_dir
    emitter.emit(
        {
            "stage": STAGE_BACKUP,
            "status": STATUS_DONE,
            "job_id": prepared.job_id,
            "tool_id": request.tool_id,
            "backup_dir": manifest.backup_dir,
            "files_done": manifest.file_count,
            "bytes_done": manifest.total_bytes,
        }
    )

    _check_cancelled(cancel_token)

    # S2: create the job's staging directory; it must not already exist.
    try:
        os.makedirs(prepared.tool_dir, exist_ok=True)
        os.mkdir(prepared.staging_parent)
    except FileExistsError:
        message = "staging directory already exists: %s" % prepared.staging_parent
        return _failure(result, ERROR_COPY, message)
    except OSError as exc:
        return _failure(
            result, ERROR_COPY, "cannot create staging directory: %s" % exc
        )

    if os.path.lexists(prepared.staging_path):
        removed, cleanup_error = _cleanup_staging(prepared, prepared.job_id)
        message = "staging path already exists: %s" % prepared.staging_path
        if cleanup_error:
            message = "%s (cleanup failed: %s)" % (message, cleanup_error)
        return _failure(result, ERROR_COPY, message, temp_removed=removed)

    # S3: robocopy into staging.
    log_path = os.path.join(prepared.staging_parent, LOG_NAME)
    try:
        copy_result = copy_to_staging(
            prepared.source_path,
            prepared.staging_path,
            log_path,
            cancel_token=cancel_token,
            transfer=transfer,
            progress_cb=progress_cb,
            job_id=prepared.job_id,
            tool_id=request.tool_id,
            sample_seconds=sample_seconds,
            expected_bytes=request.expected_bytes,
            expected_files=request.expected_files,
        )
    except MigrationCancelled:
        _cleanup_staging(prepared, prepared.job_id)
        raise
    except (TransferError, OSError) as exc:
        removed, cleanup_error = _cleanup_staging(prepared, prepared.job_id)
        message = "copy failed: %s" % exc
        if cleanup_error:
            message = "%s (cleanup failed: %s)" % (message, cleanup_error)
        return _failure(result, ERROR_COPY, message, temp_removed=removed)

    result.copy = copy_result
    if not copy_result.ok:
        removed, cleanup_error = _cleanup_staging(prepared, prepared.job_id)
        message = "robocopy exited with %d (>= 8 means failure)" % (
            copy_result.exit_code,
        )
        if cleanup_error:
            message = "%s (cleanup failed: %s)" % (message, cleanup_error)
        return _failure(result, ERROR_COPY, message, temp_removed=removed)

    _check_cancelled(cancel_token)

    # S4: authoritative verification; a mismatch never touches the source.
    emitter.emit(
        {
            "stage": STAGE_VERIFY,
            "status": STATUS_START,
            "job_id": prepared.job_id,
            "tool_id": request.tool_id,
            "source_path": prepared.source_path,
            "staging_path": prepared.staging_path,
        }
    )
    try:
        verify_result = verify_trees(
            prepared.source_path, prepared.staging_path, cancel_token=cancel_token
        )
    except MigrationCancelled:
        _cleanup_staging(prepared, prepared.job_id)
        raise

    result.verify = verify_result
    if not verify_result.ok:
        removed, cleanup_error = _cleanup_staging(prepared, prepared.job_id)
        message = verify_result.reason or "verification failed"
        if cleanup_error:
            message = "%s (cleanup failed: %s)" % (message, cleanup_error)
        emitter.emit(
            {
                "stage": STAGE_VERIFY,
                "status": STATUS_FAILED,
                "job_id": prepared.job_id,
                "tool_id": request.tool_id,
                "message": message,
                "source_file_count": verify_result.source_file_count,
                "target_file_count": verify_result.target_file_count,
                "source_bytes": verify_result.source_bytes,
                "target_bytes": verify_result.target_bytes,
            }
        )
        return _failure(result, ERROR_VERIFY, message, temp_removed=removed)

    emitter.emit(
        {
            "stage": STAGE_VERIFY,
            "status": STATUS_DONE,
            "job_id": prepared.job_id,
            "tool_id": request.tool_id,
            "source_file_count": verify_result.source_file_count,
            "target_file_count": verify_result.target_file_count,
            "source_bytes": verify_result.source_bytes,
            "target_bytes": verify_result.target_bytes,
            "staging_path": prepared.staging_path,
        }
    )
    result.ok = True
    return result


__all__ = ["prepare_copy"]
