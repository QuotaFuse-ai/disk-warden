# -*- coding: utf-8 -*-
"""Non-secret auth state and DPAPI-protected tokens (T16).

``config/auth.json`` holds *state only*: whether the welcome page was seen,
whether the Star prompt is pending/dismissed, and the public profile of the
signed-in account.  It never holds a token.

Tokens live in ``config/auth-token.bin`` encrypted with Windows DPAPI
(``CryptProtectData``).  There is deliberately **no plaintext fallback**: when
DPAPI is unavailable the token is kept in memory for the session and the UI
reports that sign-in cannot be remembered.  Writing a token as plain JSON
would contradict the architecture's credential rule.

T25's shared keystore (``src/core/ai/keystore.py``) is not on ``main`` yet, so
this module carries the same DPAPI call itself; :class:`TokenStore` is the
only seam, and switching to the shared module is a two-line change in
:meth:`TokenStore.save` / :meth:`TokenStore.load` once it lands.

Python 3.8 compatible (decision 0005).
"""

import ctypes
import json
import logging
import os
import sys

from ...core.store.atomic import (
    StoreCorruptError,
    atomic_write_bytes,
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


class SecureStoreUnavailable(RuntimeError):
    """Raised when no OS-protected storage exists for a token."""


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


# -- Windows DPAPI --------------------------------------------------------


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_ulong),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _make_blob(data):
    # type: (bytes) -> tuple
    buffer = ctypes.create_string_buffer(data, len(data))
    blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    return blob, buffer


def _dpapi_protect(data):
    # type: (bytes) -> bytes
    if not sys.platform.startswith("win"):
        raise SecureStoreUnavailable("DPAPI is only available on Windows")
    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
    except (AttributeError, OSError) as exc:
        raise SecureStoreUnavailable("DPAPI is unavailable: %s" % exc)
    blob_in, keep_in = _make_blob(data)
    blob_out = _DataBlob()
    # CRYPTPROTECT_UI_FORBIDDEN = 0x1: never show a OS prompt from the app.
    ok = crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None, 0x1, ctypes.byref(blob_out)
    )
    if not ok:
        raise SecureStoreUnavailable("CryptProtectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _dpapi_unprotect(data):
    # type: (bytes) -> bytes
    if not sys.platform.startswith("win"):
        raise SecureStoreUnavailable("DPAPI is only available on Windows")
    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
    except (AttributeError, OSError) as exc:
        raise SecureStoreUnavailable("DPAPI is unavailable: %s" % exc)
    blob_in, keep_in = _make_blob(data)
    blob_out = _DataBlob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0x1, ctypes.byref(blob_out)
    )
    if not ok:
        raise SecureStoreUnavailable("CryptUnprotectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


class TokenStore(object):
    """DPAPI-encrypted token blob; refuses to persist plaintext."""

    def __init__(self, config_dir):
        # type: (str) -> None
        self.path = os.path.join(config_dir, TOKEN_FILE)
        self._memory = None

    def available(self):
        # type: () -> bool
        """True when the platform can protect a token at rest."""
        if not sys.platform.startswith("win"):
            return False
        try:
            import ctypes as _ctypes  # noqa: F401
            return hasattr(ctypes, "windll")
        except Exception:
            return False

    def load(self):
        # type: () -> object
        if self._memory is not None:
            return self._memory
        try:
            with open(self.path, "rb") as handle:
                blob = handle.read()
        except FileNotFoundError:
            return None
        except OSError as exc:
            LOGGER.warning("token file is unreadable: %s", exc)
            return None
        try:
            payload = json.loads(_dpapi_unprotect(blob).decode("utf-8"))
        except (SecureStoreUnavailable, ValueError, UnicodeDecodeError) as exc:
            LOGGER.warning("token file cannot be decrypted: %s", exc)
            return None
        return payload if isinstance(payload, dict) else None

    def save(self, token):
        # type: (dict) -> bool
        """Persist a token; returns False when only memory storage is available.

        The plaintext blob is never written, even as a fallback.
        """
        self._memory = dict(token)
        if not self.available():
            return False
        try:
            raw = json.dumps(token, ensure_ascii=False).encode("utf-8")
            atomic_write_bytes(self.path, _dpapi_protect(raw), fsync=True)
        except (SecureStoreUnavailable, OSError, ValueError) as exc:
            LOGGER.warning("could not protect the token: %s", exc)
            return False
        return True

    def clear(self):
        # type: () -> None
        self._memory = None
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            LOGGER.warning("could not remove the token file: %s", exc)


__all__ = [
    "AUTH_STATE_FILE",
    "AuthStateStore",
    "DEFAULT_STATE",
    "SCHEMA_VERSION",
    "SecureStoreUnavailable",
    "TOKEN_FILE",
    "TokenStore",
]
