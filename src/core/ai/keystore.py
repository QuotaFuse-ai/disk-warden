# -*- coding: utf-8 -*-
"""Per-user encrypted storage for the optional AI API key (T25).

Windows DPAPI (``CryptProtectData`` / ``CryptUnprotectData``) encrypts the
key for the current Windows user on this machine.  The DPAPI blob is written
Base64-encoded to ``<config>/ai-key.bin``.  Copying that file to another
account or machine makes decryption fail; the caller reports that as a
recoverable state instead of crashing.

DPAPI is reached through :mod:`ctypes` so no third-party dependency is added.
Other platforms raise :data:`CODE_UNSUPPORTED`; callers keep reading the
``DISK_WARDEN_AI_API_KEY`` environment variable instead.

The plaintext secret never appears in a returned value, exception message or
log record produced by this module.

Python 3.8 compatible (decision 0005).
"""

import base64
import binascii
import ctypes
import os

from ..store.atomic import StoreError, atomic_write_bytes

CRYPTPROTECT_UI_FORBIDDEN = 0x01

MAX_SECRET_BYTES = 8192
MAX_FILE_BYTES = 64 * 1024

CODE_UNSUPPORTED = "unsupported"
CODE_ENCRYPT_FAILED = "encrypt_failed"
CODE_DECRYPT_FAILED = "decrypt_failed"
CODE_CORRUPT = "corrupt"
CODE_SAVE_FAILED = "save_failed"

KEY_FILE_NAME = "ai-key.bin"


class KeystoreError(Exception):
    """A key-store failure carrying a stable code, never the secret."""

    def __init__(self, code, detail=""):
        # type: (str, str) -> None
        self.code = code
        text = code
        if detail:
            text = "%s: %s" % (code, detail)
        Exception.__init__(self, text)


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_uint32),
        ("pbData", ctypes.c_void_p),
    ]


_crypt32 = None
_kernel32 = None


def is_supported():
    # type: () -> bool
    """True when DPAPI is available (Windows only in this release)."""
    return os.name == "nt"


def _win_error(prefix):
    # type: (str) -> str
    try:
        code = ctypes.get_last_error()
    except Exception:  # pragma: no cover - defensive
        code = 0
    return "%s (winerror=%s)" % (prefix, code)


def _dlls():
    # type: () -> tuple
    global _crypt32, _kernel32
    if not is_supported():
        raise KeystoreError(
            CODE_UNSUPPORTED, "local key storage is only supported on Windows"
        )
    if _crypt32 is None:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.c_wchar_p,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptProtectData.restype = ctypes.c_int
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.POINTER(ctypes.c_wchar_p),
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptUnprotectData.restype = ctypes.c_int
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        _crypt32 = crypt32
        _kernel32 = kernel32
    return _crypt32, _kernel32


def _blob_in(payload):
    # type: (bytes) -> tuple
    buffer = ctypes.create_string_buffer(payload, len(payload))
    blob = _DataBlob(len(payload), ctypes.cast(buffer, ctypes.c_void_p))
    return blob, buffer


def _protect(secret_bytes):
    # type: (bytes) -> bytes
    crypt32, kernel32 = _dlls()
    blob, _buffer = _blob_in(secret_bytes)
    out = _DataBlob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(blob),
        "Disk Warden AI key",
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out),
    )
    if not ok:
        raise KeystoreError(CODE_ENCRYPT_FAILED, _win_error("CryptProtectData failed"))
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def _unprotect(blob):
    # type: (bytes) -> bytes
    crypt32, kernel32 = _dlls()
    blob_in, _buffer = _blob_in(blob)
    out = _DataBlob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out),
    )
    if not ok:
        raise KeystoreError(
            CODE_DECRYPT_FAILED, _win_error("CryptUnprotectData failed")
        )
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def save(path, secret):
    # type: (str, str) -> None
    """Encrypt ``secret`` for the current user and write it to ``path``."""
    if not isinstance(secret, str) or not secret:
        raise KeystoreError(CODE_ENCRYPT_FAILED, "secret is required")
    raw = secret.encode("utf-8")
    if len(raw) > MAX_SECRET_BYTES:
        raise KeystoreError(CODE_ENCRYPT_FAILED, "secret is too long")
    protected = _protect(raw)
    encoded = base64.b64encode(protected)
    try:
        atomic_write_bytes(path, encoded, fsync=True)
    except (OSError, StoreError) as exc:
        raise KeystoreError(CODE_SAVE_FAILED, "cannot write the key file: %s" % exc)


def load(path):
    # type: (str) -> str
    """Decrypt the key at ``path``; empty string when the file is absent."""
    if not path or not os.path.exists(path):
        return ""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise KeystoreError(CODE_CORRUPT, "cannot read the key file: %s" % exc)
    if len(raw) > MAX_FILE_BYTES:
        raise KeystoreError(CODE_CORRUPT, "key file is too large")
    try:
        protected = base64.b64decode(raw.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise KeystoreError(CODE_CORRUPT, "key file is not valid Base64")
    if not protected:
        raise KeystoreError(CODE_CORRUPT, "key file is empty")
    plain = _unprotect(protected)
    try:
        text = plain.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise KeystoreError(CODE_CORRUPT, "key file does not contain UTF-8 text")
    if not text:
        raise KeystoreError(CODE_CORRUPT, "key file does not contain a key")
    return text


def clear(path):
    # type: (str) -> bool
    """Delete the stored key; returns True when a file was removed."""
    if not path:
        return False
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise KeystoreError(CODE_SAVE_FAILED, "cannot remove the key file: %s" % exc)


def stored(path):
    # type: (str) -> bool
    return bool(path) and os.path.isfile(path)


__all__ = [
    "CODE_CORRUPT",
    "CODE_DECRYPT_FAILED",
    "CODE_ENCRYPT_FAILED",
    "CODE_SAVE_FAILED",
    "CODE_UNSUPPORTED",
    "KEY_FILE_NAME",
    "KeystoreError",
    "MAX_SECRET_BYTES",
    "clear",
    "is_supported",
    "load",
    "save",
    "stored",
]
