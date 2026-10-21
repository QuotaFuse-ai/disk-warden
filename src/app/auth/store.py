# -*- coding: utf-8 -*-
"""Non-secret auth state and DPAPI-protected tokens (T16).

``config/auth.json`` holds *state only*: whether the welcome page was seen,
whether the Star prompt is pending/dismissed, and the public profile of the
signed-in account.  It never holds a token.

Tokens live in ``config/auth-token.bin`` encrypted with T25's shared DPAPI
keystore (``src/core/ai/keystore.py``) -- the same per-user ``CryptProtectData``
blob the AI key uses.  There is deliberately **no plaintext fallback**: when
DPAPI is unavailable the token is kept in memory for the session and the UI
reports that sign-in cannot be remembered.  Writing a token as plain JSON
would contradict the architecture's credential rule.

Python 3.8 compatible (decision 0005).
"""

import json
import logging
import os

from ...core.ai import keystore
from ...core.store.atomic import (
    StoreCorruptError,
    atomic_write_json,
    read_json_file,
)

LOGGER = logging.getLogger("disk_warden.app.auth.store")

SCHEMA_VERSION = 1
AUTH_STATE_FILE = "auth.json"
TOKEN_FILE = "auth-token.bin"

DEFAULT_STATE = {
    "schema_version": SCHEMA_VERSION,
    "welcome_seen": False,
    "star_prompt_pending": False,
    "star_prompt_dismissed": False,
    "login": None,
}

LOGIN_KEYS = (
    "provider",
    "login",
    "name",
    "avatar_url",
    "starred",
    "authenticated_at",
    "starred_at",
)


def _sanitize_login(raw):
    # type: (object) -> object
    if not isinstance(raw, dict):
        return None
    login = {}
    for key in LOGIN_KEYS:
        value = raw.get(key)
        if value is None:
            continue
        if key in ("starred",):
            if isinstance(value, bool):
                login[key] = value
        elif isinstance(value, str):
            login[key] = value[:200]
    if not login.get("provider") or not login.get("login"):
        return None
    login["starred"] = bool(login.get("starred", False))
    return login


def _sanitize_state(raw):
    # type: (object) -> dict
    state = dict(DEFAULT_STATE)
    if not isinstance(raw, dict):
        return state
    for key in ("welcome_seen", "star_prompt_pending", "star_prompt_dismissed"):
        if isinstance(raw.get(key), bool):
            state[key] = raw[key]
    state["login"] = _sanitize_login(raw.get("login"))
    return state


class AuthStateStore(object):
    """``<config>/auth.json`` -- state and public profile, never secrets."""

    def __init__(self, config_dir):
        # type: (str) -> None
        self.path = os.path.join(config_dir, AUTH_STATE_FILE)

    def load(self):
        # type: () -> dict
        try:
            payload = read_json_file(self.path)
        except FileNotFoundError:
            return dict(DEFAULT_STATE)
        except (StoreCorruptError, OSError) as exc:
            LOGGER.warning("auth state is unreadable (%s); starting fresh", exc)
            return dict(DEFAULT_STATE)
        return _sanitize_state(payload)

    def save(self, state):
        # type: (dict) -> dict
        clean = _sanitize_state(state)
        clean["schema_version"] = SCHEMA_VERSION
        try:
            atomic_write_json(self.path, clean, backup=True)
        except OSError as exc:
            LOGGER.warning("could not persist auth state: %s", exc)
        return clean


# -- token store ----------------------------------------------------------


class TokenStore(object):
    """Token payload in T25's DPAPI blob; refuses to persist plaintext.

    The payload is a JSON object (``access_token``, ``scope`` ...); the whole
    object is encrypted as one secret by the shared keystore.  When DPAPI is
    unavailable the payload is kept in memory for the session only.
    """

    def __init__(self, config_dir):
        # type: (str) -> None
        self.path = os.path.join(config_dir, TOKEN_FILE)
        self._memory = None

    def available(self):
        # type: () -> bool
        """True when the platform can protect a token at rest."""
        return keystore.is_supported()

    def load(self):
        # type: () -> object
        if self._memory is not None:
            return self._memory
        try:
            text = keystore.load(self.path)
        except keystore.KeystoreError as exc:
            LOGGER.warning("stored token is unavailable (%s)", exc.code)
            return None
        if not text:
            return None
        try:
            payload = json.loads(text)
        except ValueError:
            LOGGER.warning("stored token is not valid JSON")
            return None
        if not isinstance(payload, dict):
            return None
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            return None
        return payload

    def save(self, token):
        # type: (dict) -> bool
        """Persist a token; returns False when only memory storage is available.

        The plaintext blob is never written, even as a fallback.
        """
        self._memory = dict(token)
        if not self.available():
            return False
        try:
            raw = json.dumps(token, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            LOGGER.warning("token payload is not serialisable")
            return False
        try:
            keystore.save(self.path, raw)
        except keystore.KeystoreError as exc:
            LOGGER.warning("could not protect the token (%s)", exc.code)
            return False
        return True

    def clear(self):
        # type: () -> None
        self._memory = None
        try:
            keystore.clear(self.path)
        except keystore.KeystoreError as exc:
            LOGGER.warning("could not remove the token file (%s)", exc.code)


__all__ = [
    "AUTH_STATE_FILE",
    "AuthStateStore",
    "DEFAULT_STATE",
    "SCHEMA_VERSION",
    "TOKEN_FILE",
    "TokenStore",
]
