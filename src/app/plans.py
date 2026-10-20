# -*- coding: utf-8 -*-
"""In-memory plans and hash-bound confirm tokens (architecture §5.5).

The UI never sends paths for writes: it sends a ``plan_id`` plus the
``confirm_token`` it received from ``plan.preview``/``rollback.preview``.  The
token is derived from a canonical hash of the plan payload, so a plan that was
tampered with (or re-created) cannot be confirmed with an old token.
"""

import hashlib
import json
import secrets
import threading
import time
import uuid

from .errors import invalid, not_found

PLAN_TTL_SECONDS = 300


def _canonical(payload):
    # type: (object) -> str
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _content_hash(payload):
    # type: (object) -> str
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


class PlanRegistry(object):
    """Plans live for five minutes and are consumed exactly once."""

    def __init__(self, ttl_seconds=PLAN_TTL_SECONDS, clock=None):
        # type: (int, object) -> None
        self.ttl_seconds = max(1, int(ttl_seconds))
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._plans = {}

    def create(self, kind, payload):
        # type: (str, dict) -> dict
        """Register ``payload`` and return it with plan identity attached."""
        body = json.loads(_canonical(payload))
        content_hash = _content_hash(body)
        plan_id = uuid.uuid4().hex
        token = hashlib.sha256(
            (content_hash + ":" + plan_id + ":" + secrets.token_hex(16)).encode("utf-8")
        ).hexdigest()
        now = self._clock()
        entry = {
            "kind": kind,
            "payload": body,
            "content_hash": content_hash,
            "token": token,
            "created_at": now,
            "expires_at": now + self.ttl_seconds,
        }
        with self._lock:
            self._prune_locked(now)
            self._plans[plan_id] = entry
        public = dict(body)
        public.update(
            {
                "plan_id": plan_id,
                "kind": kind,
                "confirm_token": token,
                "expires_at": _stamp(entry["expires_at"]),
            }
        )
        return public

    def peek(self, plan_id):
        # type: (str) -> dict
        with self._lock:
            entry = self._plans.get(plan_id)
        if entry is None:
            raise not_found("plan not found or already used", plan_id=plan_id)
        return json.loads(_canonical(entry["payload"]))

    def consume(self, plan_id, token):
        # type: (str, str) -> dict
        """Validate and remove a plan; returns its payload."""
        if not isinstance(plan_id, str) or not plan_id:
            raise invalid("plan_id is required")
        if not isinstance(token, str) or not token:
            raise invalid("confirm_token is required")
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            entry = self._plans.get(plan_id)
            if entry is None:
                raise not_found("plan not found, expired or already used", plan_id=plan_id)
            if now > entry["expires_at"]:
                self._plans.pop(plan_id, None)
                raise invalid("plan expired; run the preview again", plan_id=plan_id)
            if not secrets.compare_digest(str(entry["token"]), token):
                raise invalid("confirm token does not match this plan", plan_id=plan_id)
            if _content_hash(entry["payload"]) != entry["content_hash"]:
                raise invalid("plan content changed after preview", plan_id=plan_id)
            self._plans.pop(plan_id, None)
            return json.loads(_canonical(entry["payload"]))

    def prune(self):
        # type: () -> int
        with self._lock:
            return self._prune_locked(self._clock())

    def _prune_locked(self, now):
        # type: (float) -> int
        expired = [
            plan_id
            for plan_id, entry in self._plans.items()
            if now > entry["expires_at"]
        ]
        for plan_id in expired:
            self._plans.pop(plan_id, None)
        return len(expired)


def _stamp(epoch):
    # type: (float) -> str
    from datetime import datetime

    return datetime.utcfromtimestamp(epoch).replace(microsecond=0).isoformat() + "Z"
