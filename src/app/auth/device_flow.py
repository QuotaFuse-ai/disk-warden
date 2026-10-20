# -*- coding: utf-8 -*-
"""GitHub device-flow state machine (RFC 8628, T16).

The host never listens on a port: GitHub hands back a short ``user_code``,
the human types it on ``https://github.com/login/device``, and this module
polls the token endpoint in a daemon thread.  Nothing here needs a client
secret, ``state`` or PKCE -- the device code itself is the credential and it
never leaves the process.

States::

    pending -> authenticated
            -> failed      (access_denied, network, token rejection)
            -> cancelled   (user pressed cancel, or the app is closing)
            -> expired     (GitHub's ``expires_in`` elapsed)

The ``device_code`` is deliberately excluded from :meth:`DeviceFlow.status`;
only the UI-facing ``user_code`` is exposed.

Python 3.8 compatible (decision 0005).
"""

import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from ..errors import BridgeError

LOGGER = logging.getLogger("disk_warden.app.auth.device_flow")

STATE_PENDING = "pending"
STATE_AUTHENTICATED = "authenticated"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
STATE_EXPIRED = "expired"

ACTIVE_STATES = (STATE_PENDING,)
TERMINAL_STATES = (STATE_AUTHENTICATED, STATE_FAILED, STATE_CANCELLED, STATE_EXPIRED)

#: Consecutive transport failures tolerated before the flow reports failure.
MAX_NETWORK_FAILURES = 3
#: RFC 8628 §3.5: ``slow_down`` asks for at least 5 extra seconds.
SLOW_DOWN_STEP = 5.0
MAX_INTERVAL = 60.0


def _utc_now():
    # type: () -> str
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _iso_in(seconds):
    # type: (float) -> str
    moment = datetime.now(timezone.utc) + timedelta(seconds=max(0.0, float(seconds)))
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


class DeviceFlow(object):
    """One in-flight device authorization."""

    def __init__(self, provider, device, client, opener=None, monotonic=None):
        # type: (object, dict, object, object, object) -> None
        self.provider = provider
        self.flow_id = "flow-%s" % uuid.uuid4().hex[:12]
        self._device = dict(device)
        self._client = client
        self._opener = opener
        self._monotonic = monotonic or time.monotonic
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._completion = None
        self._network_failures = 0

        self._state = STATE_PENDING
        self._error = None
        self._started_at = _utc_now()
        self._updated_at = self._started_at
        self._expires_in = float(self._device.get("expires_in") or 900)
        self._deadline = self._monotonic() + self._expires_in
        self._expires_at = _iso_in(self._expires_in)
        self._interval = float(self._device.get("interval") or 5)

    # -- public surface ---------------------------------------------------

    def set_completion(self, callback):
        # type: (object) -> None
        """``callback(token, payload)`` runs on the poll thread."""
        self._completion = callback

    def start(self):
        # type: () -> None
        """Open the verification page, then start polling."""
        uri = self._device.get("verification_uri") or ""
        opener = self._opener
        if callable(opener) and uri:
            try:
                opener(uri)
            except Exception as exc:  # a missing browser must not fail login
                LOGGER.info("could not open the verification page: %s", exc)
        self._thread = threading.Thread(
            target=self._run,
            name="device-flow-%s" % self.flow_id,
            daemon=True,
        )
        self._thread.start()

    def status(self):
        # type: () -> dict
        with self._lock:
            return {
                "flow_id": self.flow_id,
                "provider": self.provider.provider,
                "state": self._state,
                "user_code": self._device.get("user_code", ""),
                "verification_uri": self._device.get("verification_uri", ""),
                "expires_in": int(self._expires_in),
                "expires_at": self._expires_at,
                "interval": int(self._interval),
                "started_at": self._started_at,
                "updated_at": self._updated_at,
                "error": dict(self._error) if self._error else None,
            }

    def cancel(self):
        # type: () -> bool
        """Stop polling; the flow ends as ``cancelled`` unless it already ended."""
        self._stop.set()
        self._settle(STATE_CANCELLED)
        return True

    def close(self):
        # type: () -> None
        self._stop.set()
        self._settle(STATE_CANCELLED, override=False)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        self._thread = None

    def mark_authenticated(self, login, starred):
        # type: (dict, bool) -> None
        """Called by the manager once the profile and star state are known."""
        with self._lock:
            if self._state != STATE_PENDING:
                return
            self._state = STATE_AUTHENTICATED
            self._error = None
            self._updated_at = _utc_now()
        self._stop.set()

    def mark_failed(self, code, reason, detail=None):
        # type: (str, str, object) -> None
        self._fail(code, reason, detail)

    # -- internals --------------------------------------------------------

    def _settle(self, state, override=True):
        # type: (str, bool) -> None
        with self._lock:
            if not override and self._state in TERMINAL_STATES:
                return
            if self._state in (STATE_AUTHENTICATED, STATE_FAILED, STATE_EXPIRED):
                return
            self._state = state
            self._updated_at = _utc_now()

    def _fail(self, code, reason, detail=None):
        # type: (str, str, object) -> None
        with self._lock:
            if self._state in TERMINAL_STATES:
                return
            self._state = STATE_FAILED
            self._error = {
                "code": code,
                "reason": str(reason or "")[:80],
                "message_key": "error." + code,
                "detail": dict(detail) if isinstance(detail, dict) else {},
            }
            self._updated_at = _utc_now()
        self._stop.set()

    def _expire(self):
        # type: () -> None
        with self._lock:
            if self._state in TERMINAL_STATES:
                return
            self._state = STATE_EXPIRED
            self._updated_at = _utc_now()
        self._stop.set()

    def _run(self):
        # type: () -> None
        while not self._stop.is_set():
            if self._stop.wait(self._interval):
                return
            with self._lock:
                if self._state != STATE_PENDING:
                    return
            if self._monotonic() >= self._deadline:
                self._expire()
                return
            try:
                payload = self._client.poll_device_token(self._device["device_code"])
            except BridgeError as exc:
                self._network_failures += 1
                if self._network_failures >= MAX_NETWORK_FAILURES:
                    detail = dict(exc.detail)
                    detail.setdefault("attempts", self._network_failures)
                    self._fail(exc.code, exc.reason or "network", detail)
                    return
                continue
            except Exception as exc:  # never let a poll thread kill the app
                LOGGER.error("device poll crashed: %s", type(exc).__name__)
                self._fail("AUTH_FAILED", "internal_error")
                return

            self._network_failures = 0
            token = payload.get("access_token")
            if isinstance(token, str) and token:
                self._deliver(token, payload)
                return

            error = str(payload.get("error") or "")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                with self._lock:
                    self._interval = min(MAX_INTERVAL, self._interval + SLOW_DOWN_STEP)
                continue
            if error == "expired_token":
                self._expire()
                return
            if error == "access_denied":
                self._fail("AUTH_CANCELLED", "access_denied")
                return
            self._fail("AUTH_FAILED", error or "token_rejected")

    def _deliver(self, token, payload):
        # type: (str, dict) -> None
        callback = self._completion
        if not callable(callback):
            self._fail("AUTH_FAILED", "no_completion_handler")
            return
        try:
            callback(token, payload)
        except BridgeError as exc:
            detail = dict(exc.detail)
            detail.setdefault("stage", "profile")
            self._fail(exc.code, exc.reason or "profile", detail)
        except Exception as exc:  # never log the token or the payload
            LOGGER.error("sign-in completion failed: %s", type(exc).__name__)
            self._fail("AUTH_FAILED", "internal_error")


__all__ = [
    "ACTIVE_STATES",
    "DeviceFlow",
    "MAX_INTERVAL",
    "MAX_NETWORK_FAILURES",
    "SLOW_DOWN_STEP",
    "STATE_AUTHENTICATED",
    "STATE_CANCELLED",
    "STATE_EXPIRED",
    "STATE_FAILED",
    "STATE_PENDING",
    "TERMINAL_STATES",
]
