# -*- coding: utf-8 -*-
"""Progress events shared by the second half of the migration (T10).

The payload shape matches engine A: ``stage``, ``status``, ``job_id``,
``tool_id`` and ``elapsed_seconds`` plus stage-specific fields.  Callbacks are
optional and never receive more than one payload per call.

Python 3.8 compatible (decision 0005).
"""

import time


class Emitter(object):
    def __init__(self, callback, job_id="", tool_id=""):
        # type: (object, str, str) -> None
        self._callback = callback
        self._job_id = job_id
        self._tool_id = tool_id
        self._started = time.monotonic()

    def emit(self, stage, status, **fields):
        # type: (str, str, object) -> None
        if self._callback is None:
            return
        payload = {
            "stage": stage,
            "status": status,
            "job_id": self._job_id,
            "tool_id": self._tool_id,
            "elapsed_seconds": round(time.monotonic() - self._started, 3),
        }
        payload.update(fields)
        self._callback(payload)


__all__ = ["Emitter"]
