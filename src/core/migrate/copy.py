# -*- coding: utf-8 -*-
"""Copy source data into the job's staging directory (S2-S3).

The business layer calls the platform transfer primitive, never ``subprocess``
directly.  Progress during the copy is approximate: the staging tree is
sampled every few seconds, and only the verification step is authoritative
(architecture 6.6).

Python 3.8 compatible (decision 0005).
"""

import os
import stat as stat_module
import threading
import time

from ...platform import (
    TransferCancelled,
    is_reparse_stat,
    run_robocopy,
)
from .models import (
    STAGE_COPY,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PROGRESS,
    STATUS_START,
    CopyResult,
    MigrationCancelled,
)

DEFAULT_SAMPLE_SECONDS = 2.0
LARGE_TREE_FILES = 200000
MAX_SAMPLE_SECONDS = 10.0
LOG_NAME = "robocopy.log"


def sample_tree(root):
    # type: (str) -> tuple
    """Return ``(bytes, files)`` below ``root`` without following links.

    Read errors are ignored on purpose: this is a progress sample, and the
    verify step will report them authoritatively.
    """
    try:
        root_info = os.lstat(root)
    except OSError:
        return 0, 0
    if is_reparse_stat(root_info):
        return 0, 0
    if not stat_module.S_ISDIR(root_info.st_mode):
        return root_info.st_size, 1

    total_bytes = 0
    file_count = 0
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                children = list(entries)
        except OSError:
            continue
        for entry in children:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if is_reparse_stat(info):
                continue
            if stat_module.S_ISDIR(info.st_mode):
                stack.append(entry.path)
            elif stat_module.S_ISREG(info.st_mode):
                total_bytes += info.st_size
                file_count += 1
    return total_bytes, file_count


class _Sampler(object):
    """Samples staging usage while robocopy runs; never raises to the caller."""

    def __init__(
        self,
        root,
        progress_cb,
        sample_seconds,
        job_id,
        tool_id,
        expected_bytes,
        expected_files,
    ):
        # type: (str, object, float, str, str, object, object) -> None
        self._root = root
        self._callback = progress_cb
        self._interval = max(0.05, sample_seconds)
        self._job_id = job_id
        self._tool_id = tool_id
        self._expected_bytes = expected_bytes
        self._expected_files = expected_files
        self._stop = threading.Event()
        self._thread = None  # type: object
        self.sampled_bytes = 0
        self.sampled_files = 0

    def start(self):
        # type: () -> None
        if self._callback is None:
            return
        self._thread = threading.Thread(
            target=self._run, name="dw-copy-progress", daemon=True
        )
        self._thread.start()

    def stop(self):
        # type: () -> None
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.sampled_bytes, self.sampled_files = sample_tree(self._root)

    def _run(self):
        # type: () -> None
        interval = self._interval
        while not self._stop.wait(interval):
            sampled_bytes, sampled_files = sample_tree(self._root)
            if sampled_files > LARGE_TREE_FILES:
                interval = min(MAX_SAMPLE_SECONDS, interval * 2)
            self.sampled_bytes = sampled_bytes
            self.sampled_files = sampled_files
            self._emit()

    def _emit(self):
        # type: () -> None
        if self._callback is None:
            return
        payload = {
            "stage": STAGE_COPY,
            "status": STATUS_PROGRESS,
            "job_id": self._job_id,
            "tool_id": self._tool_id,
            "staging_path": self._root,
            "bytes_done": self.sampled_bytes,
            "files_done": self.sampled_files,
        }
        if self._expected_bytes is not None:
            payload["bytes_total"] = self._expected_bytes
        if self._expected_files is not None:
            payload["files_total"] = self._expected_files
        self._callback(payload)


def copy_to_staging(
    source_path,
    staging_path,
    log_path,
    cancel_token=None,
    transfer=None,
    progress_cb=None,
    job_id="",
    tool_id="",
    sample_seconds=DEFAULT_SAMPLE_SECONDS,
    expected_bytes=None,
    expected_files=None,
):
    # type: (str, str, str, object, object, object, str, str, float, object, object) -> CopyResult
    """Run the platform transfer and return its raw outcome.

    ``transfer`` defaults to the platform ``run_robocopy``.  Raising
    ``MigrationCancelled`` means the token fired; the engine removes staging.
    """
    if transfer is None:
        transfer = run_robocopy

    if progress_cb is not None:
        progress_cb(
            {
                "stage": STAGE_COPY,
                "status": STATUS_START,
                "job_id": job_id,
                "tool_id": tool_id,
                "source_path": source_path,
                "staging_path": staging_path,
                "bytes_total": expected_bytes,
                "files_total": expected_files,
            }
        )

    sampler = _Sampler(
        staging_path,
        progress_cb,
        sample_seconds,
        job_id,
        tool_id,
        expected_bytes,
        expected_files,
    )
    started = time.monotonic()
    sampler.start()
    try:
        exit_code = transfer(source_path, staging_path, log_path, cancel_token)
    except TransferCancelled:
        raise MigrationCancelled("migration cancelled during copy")
    finally:
        sampler.stop()
    duration = time.monotonic() - started

    result = CopyResult(
        exit_code=exit_code,
        staging_path=staging_path,
        log_path=log_path,
        duration_seconds=round(duration, 3),
        sampled_bytes=sampler.sampled_bytes,
        sampled_files=sampler.sampled_files,
    )
    if progress_cb is not None:
        progress_cb(
            {
                "stage": STAGE_COPY,
                "status": STATUS_DONE if result.ok else STATUS_FAILED,
                "job_id": job_id,
                "tool_id": tool_id,
                "staging_path": staging_path,
                "exit_code": exit_code,
                "bytes_done": sampler.sampled_bytes,
                "files_done": sampler.sampled_files,
                "bytes_total": expected_bytes,
                "files_total": expected_files,
                "elapsed_seconds": result.duration_seconds,
            }
        )
    return result


__all__ = [
    "DEFAULT_SAMPLE_SECONDS",
    "LOG_NAME",
    "copy_to_staging",
    "sample_tree",
]
