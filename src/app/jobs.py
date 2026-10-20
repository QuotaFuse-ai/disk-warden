# -*- coding: utf-8 -*-
"""Long-task runner for scans, migrations and rollbacks (architecture §5.2).

One worker thread per job; long tasks are submit-then-poll.  Scans and writes
are mutually exclusive (``BUSY``) and writes are additionally serialised by a
single-writer lock, so two migrations can never race on the same journal.
"""

import logging
import threading
import time
import traceback
import uuid
from collections import OrderedDict
from datetime import datetime

from ..core.migrate.models import MigrationCancelled
from ..core.scan.cancel import ScanCancelled
from .errors import BridgeError

LOGGER = logging.getLogger("disk_warden.app.jobs")

STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_CANCELLING = "cancelling"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"

ACTIVE_STATES = (STATE_QUEUED, STATE_RUNNING, STATE_CANCELLING)

SCAN = "scan"
MIGRATE = "migrate"
ROLLBACK = "rollback"
RECOVER = "recover"

WRITE_KINDS = (MIGRATE, ROLLBACK, RECOVER)

MAX_FINISHED_JOBS = 50


def _now():
    # type: () -> str
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


class Job(object):
    """Mutable job record; only the runner and its worker touch it."""

    def __init__(self, job_id, kind, title=""):
        # type: (str, str, str) -> None
        self.job_id = job_id
        self.kind = kind
        self.title = title or kind
        self.state = STATE_QUEUED
        self.step = ""
        self.progress = {}
        self.summary = None
        self.error = None
        self.warnings = []
        self.created_at = _now()
        self.started_at = ""
        self.updated_at = self.created_at
        self.finished_at = ""

    @property
    def active(self):
        # type: () -> bool
        return self.state in ACTIVE_STATES

    def to_dict(self):
        # type: () -> dict
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "title": self.title,
            "state": self.state,
            "step": self.step,
            "progress": dict(self.progress),
            "summary": self.summary,
            "error": self.error,
            "warnings": list(self.warnings),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
        }


class JobRunner(object):
    """Thread-per-job executor with cancel tokens and a 500-entry event bus."""

    def __init__(self, bus, max_finished=MAX_FINISHED_JOBS):
        # type: (object, int) -> None
        self.bus = bus
        self.max_finished = max(1, int(max_finished))
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._jobs = OrderedDict()
        self._tokens = {}
        self._threads = {}
        self._done = {}

    # -- queries ---------------------------------------------------------
    def active_jobs(self, kinds=None):
        # type: (object) -> list
        with self._lock:
            jobs = [job for job in self._jobs.values() if job.active]
        if kinds:
            allowed = set(kinds)
            jobs = [job for job in jobs if job.kind in allowed]
        return jobs

    def has_active(self, kinds=None):
        # type: (object) -> bool
        return bool(self.active_jobs(kinds))

    def has_write(self):
        # type: () -> bool
        return self.has_active(WRITE_KINDS)

    def get(self, job_id):
        # type: (str) -> Job
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise BridgeError("NOT_FOUND", "job not found", {"job_id": job_id})
        return job

    def status(self, job_id, recent=20):
        # type: (str, int) -> dict
        job = self.get(job_id)
        return {"job": job.to_dict(), "recent_events": self.bus.recent(recent, job_id)}

    def list(self, active_only=False):
        # type: (bool) -> list
        with self._lock:
            jobs = list(self._jobs.values())
        if active_only:
            jobs = [job for job in jobs if job.active]
        return [job.to_dict() for job in jobs]

    def wait(self, job_id, timeout=60.0):
        # type: (str, float) -> dict
        """Block until a job is no longer active (test/CLI helper)."""
        job = self.get(job_id)
        event = self._done.setdefault(job_id, threading.Event())
        event.wait(timeout)
        return job.to_dict()

    # -- commands --------------------------------------------------------
    def start(self, kind, worker, title="", check_busy=True):
        # type: (str, object, str, bool) -> str
        """Run ``worker(job, token, emit)`` in a thread and return the job id."""
        if check_busy:
            if kind == SCAN:
                if self.has_active():
                    raise BridgeError(
                        "BUSY", "a job is already running; wait or cancel it first"
                    )
            elif self.has_active():
                raise BridgeError(
                    "BUSY", "a job is already running; wait or cancel it first"
                )

        job_id = uuid.uuid4().hex[:16]
        job = Job(job_id, kind, title)
        token = threading.Event()
        with self._lock:
            self._jobs[job_id] = job
            self._tokens[job_id] = token
            self._done[job_id] = threading.Event()
            self._trim_locked()

        thread = threading.Thread(
            target=self._run,
            args=(job, token, worker, kind in WRITE_KINDS),
            name="dw-%s-%s" % (kind, job_id[:6]),
        )
        thread.daemon = True
        with self._lock:
            self._threads[job_id] = thread
        thread.start()
        return job_id

    def cancel(self, job_id):
        # type: (str) -> bool
        """Request cancellation; returns False when the job already finished."""
        job = self.get(job_id)
        with self._lock:
            token = self._tokens.get(job_id)
            if token is None or not job.active:
                return False
            token.set()
            if job.state == STATE_QUEUED:
                job.state = STATE_CANCELLING
                job.updated_at = _now()
        self.bus.emit(
            "job.state",
            {"job_id": job_id, "state": job.state, "step": job.step},
        )
        return True

    def shutdown(self, timeout=5.0):
        # type: (float) -> None
        """Signal every active job and wait briefly for their threads."""
        with self._lock:
            tokens = list(self._tokens.values())
            threads = list(self._threads.values())
        for token in tokens:
            token.set()
        deadline = time.time() + max(0.0, float(timeout))
        for thread in threads:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            thread.join(remaining)

    # -- internals -------------------------------------------------------
    def _run(self, job, token, worker, is_write):
        acquired = False
        try:
            if is_write:
                self._write_lock.acquire()
                acquired = True
            if token.is_set():
                self._finish(job, STATE_CANCELLED, error=None)
                return
            job.state = STATE_RUNNING
            job.started_at = _now()
            job.updated_at = job.started_at
            self.bus.emit(
                "job.state",
                {"job_id": job.job_id, "state": job.state, "step": job.step},
            )

            def emit(event_type, payload=None, throttle=False):
                data = dict(payload or {})
                data.setdefault("job_id", job.job_id)
                event = self.bus.emit(event_type, data, throttle=throttle)
                if event_type == "job.progress" and isinstance(payload, dict):
                    job.progress = dict(payload)
                return event

            summary = worker(job, token, emit)
            if token.is_set() and job.state != STATE_CANCELLED:
                job.state = STATE_CANCELLED
            else:
                job.state = STATE_DONE
            job.summary = summary if isinstance(summary, dict) else None
            self._finish(job, job.state, error=None, summary=job.summary)
        except (ScanCancelled, MigrationCancelled) as exc:
            self._finish(
                job,
                STATE_CANCELLED,
                error=None,
                summary={"reason": str(exc) or "cancelled"},
            )
        except BridgeError as exc:
            self._finish(job, STATE_FAILED, error=exc.to_error())
        except Exception as exc:  # unexpected failures must not kill the app
            LOGGER.error(
                "job %s (%s) crashed: %s\n%s",
                job.job_id,
                job.kind,
                exc,
                traceback.format_exc(),
            )
            self._finish(
                job,
                STATE_FAILED,
                error=BridgeError(
                    "INTERNAL", "%s: %s" % (type(exc).__name__, exc)
                ).to_error(),
            )
        finally:
            if acquired:
                self._write_lock.release()
            with self._lock:
                self._tokens.pop(job.job_id, None)
                self._threads.pop(job.job_id, None)
            done = self._done.get(job.job_id)
            if done is not None:
                done.set()

    def _finish(self, job, state, error=None, summary=None):
        job.state = state
        job.error = error
        if summary is not None:
            job.summary = summary
        job.finished_at = _now()
        job.updated_at = job.finished_at
        self.bus.emit(
            "job.state",
            {"job_id": job.job_id, "state": state, "step": job.step},
        )
        if state == STATE_DONE and isinstance(summary, dict):
            self.bus.emit(
                "job.done",
                {"job_id": job.job_id, "state": state, "summary": summary},
            )

    def _trim_locked(self):
        # type: () -> None
        finished = [
            job_id
            for job_id, job in self._jobs.items()
            if not job.active
        ]
        overflow = len(finished) - self.max_finished
        for job_id in finished[: max(0, overflow)]:
            self._jobs.pop(job_id, None)
            self._done.pop(job_id, None)
