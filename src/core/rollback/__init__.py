# -*- coding: utf-8 -*-
"""One-click rollback (T11, architecture 6.7).

The package exposes three entry points and the request/result models:

* :func:`precheck_rollback` -- observation-only decision (rollback,
  already-rolled-back, residual copy or blocked) with no filesystem changes.
* :func:`rollback` -- the full flow: remove junction, copy the target back,
  verify, rename into place, restore configuration, delete only this job's
  target and record history/learned rules.
* :func:`recover_rollback` -- converge an interrupted rollback after restart
  using the journal plus the filesystem, never deleting without proof.

Python 3.8 compatible (decision 0005).
"""

from . import models as _models
from .config import restore_configs
from .engine import precheck_rollback, rollback
from .models import *  # noqa: F401,F403 - the frozen public model surface
from .recovery import RollbackObservation, observe, recover_rollback

__all__ = list(_models.__all__) + [
    "RollbackObservation",
    "observe",
    "precheck_rollback",
    "recover_rollback",
    "restore_configs",
    "rollback",
]
