# -*- coding: utf-8 -*-
"""Bounded event bus for UI polling (architecture §5.3).

The UI polls ``events.poll`` every 250 ms; polling is authoritative.  The
optional ``evaluate_js`` poke carries no payload, so progress data never gets
spliced into JavaScript source.
"""

import threading
import time
from collections import deque
from datetime import datetime

CAPACITY = 500
# At most four progress events per second per (job, step).
MIN_PROGRESS_INTERVAL = 0.25


def utc_stamp():
    # type: () -> str
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


class EventBus(object):
    """Thread-safe ring buffer with monotonic ``seq`` values."""

    def __init__(self, capacity=CAPACITY, min_interval=MIN_PROGRESS_INTERVAL):
        # type: (int, float) -> None
        self._capacity = max(1, int(capacity))
        self._min_interval = max(0.0, float(min_interval))
        self._lock = threading.Lock()
        self._events = deque(maxlen=self._capacity)
        self._seq = 0
        self._last_emit = {}
        self._dropped = 0

    @property
    def seq(self):
        # type: () -> int
        with self._lock:
            return self._seq

    def emit(self, event_type, payload=None, throttle=False, drop_silently=False):
        # type: (str, object, bool, bool) -> object
        """Append one event; returns it, or ``None`` when throttled.

        ``throttle`` is per ``(event_type, job_id, step)``.
        """
        data = dict(payload) if isinstance(payload, dict) else {}
        key = (
            event_type,
            str(data.get("job_id", "")),
            str(data.get("step", "")),
        )
        now = time.monotonic()
        with self._lock:
            if throttle:
                previous = self._last_emit.get(key)
                if previous is not None and now - previous < self._min_interval:
                    return None
                self._last_emit[key] = now
            elif len(self._last_emit) > 512:
                # Progress keys are bounded by the number of live jobs; trim
                # stale entries when the map grows unexpectedly.
                self._last_emit.clear()
            self._seq += 1
            event = {
                "seq": self._seq,
                "type": event_type,
                "at": utc_stamp(),
            }
            event.update(data)
            if len(self._events) == self._capacity:
                self._dropped += 1
            self._events.append(event)
            return dict(event)

    def poll(self, since_seq=0):
        # type: (int) -> dict
        """Return events with ``seq > since_seq`` and the new high-water mark."""
        try:
            since = int(since_seq or 0)
        except (TypeError, ValueError):
            since = 0
        with self._lock:
            events = [dict(item) for item in self._events if item["seq"] > since]
            return {"events": events, "next_seq": self._seq}

    def recent(self, limit=20, job_id=None):
        # type: (int, str) -> list
        """Newest events first, optionally filtered to one job."""
        try:
            count = max(1, int(limit))
        except (TypeError, ValueError):
            count = 20
        with self._lock:
            items = list(self._events)
        if job_id:
            items = [item for item in items if item.get("job_id") == job_id]
        return [dict(item) for item in items[-count:][::-1]]

    def reset(self):
        # type: () -> None
        with self._lock:
            self._events.clear()
            self._last_emit.clear()
            self._seq = 0
            self._dropped = 0
