# -*- coding: utf-8 -*-
"""Windows mandatory-integrity-level diagnostics.

Windows starts an image at the integrity label carried by the executable file
when that label is higher than the label the parent passes down.  A package
extracted into a Low-labelled folder therefore runs Low even when it is started
from an ordinary Medium window, and mandatory no-write-up then refuses every
create, rename and delete inside a normal (Medium) folder such as
``%APPDATA%``.  The failure is ``ERROR_ACCESS_DENIED`` (WinError 5) with no
open handle involved, so Restart Manager finds nothing and the real cause is
invisible unless it is checked directly.

Everything here is diagnostic: no function raises, and nothing is modified.

Python 3.8 compatible (decision 0005).
"""

import ctypes
import os
import sys
from ctypes import wintypes

UNTRUSTED = 0x0000
LOW = 0x1000
MEDIUM = 0x2000
HIGH = 0x3000
SYSTEM = 0x4000

_TOKEN_QUERY = 0x0008
_TOKEN_INTEGRITY_LEVEL = 25
_TOKEN_MANDATORY_POLICY = 27
_NO_WRITE_UP = 0x00000001

_LABEL_SECURITY_INFORMATION = 0x00000010
_SE_FILE_OBJECT = 1
_SYSTEM_MANDATORY_LABEL_ACE_TYPE = 0x11

_LEVEL_NAMES = {
    UNTRUSTED: "Untrusted",
    LOW: "Low",
    MEDIUM: "Medium",
    HIGH: "High",
    SYSTEM: "System",
}

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)


class _SID(ctypes.Structure):
    _fields_ = [
        ("Revision", ctypes.c_ubyte),
        ("SubAuthorityCount", ctypes.c_ubyte),
        ("IdentifierAuthority", ctypes.c_ubyte * 6),
        ("SubAuthority", wintypes.DWORD * 1),
    ]


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", wintypes.LPVOID), ("Attributes", wintypes.DWORD)]


class _TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", _SID_AND_ATTRIBUTES)]


class _ACL(ctypes.Structure):
    _fields_ = [
        ("AclRevision", wintypes.BYTE),
        ("Sbz1", wintypes.BYTE),
        ("AclSize", wintypes.WORD),
        ("AceCount", wintypes.WORD),
        ("Sbz2", wintypes.WORD),
    ]


class _ACE_HEADER(ctypes.Structure):
    _fields_ = [
        ("AceType", ctypes.c_ubyte),
        ("AceFlags", ctypes.c_ubyte),
        ("AceSize", wintypes.WORD),
    ]


class _SYSTEM_MANDATORY_LABEL_ACE(ctypes.Structure):
    _fields_ = [
        ("Header", _ACE_HEADER),
        ("Mask", wintypes.DWORD),
        ("SidStart", wintypes.DWORD),
    ]


_GetCurrentProcess = _kernel32.GetCurrentProcess
_GetCurrentProcess.argtypes = []
_GetCurrentProcess.restype = wintypes.HANDLE

_OpenProcessToken = _advapi32.OpenProcessToken
_OpenProcessToken.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.HANDLE),
]
_OpenProcessToken.restype = wintypes.BOOL

_GetTokenInformation = _advapi32.GetTokenInformation
_GetTokenInformation.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
_GetTokenInformation.restype = wintypes.BOOL

_CloseHandle = _kernel32.CloseHandle
_CloseHandle.argtypes = [wintypes.HANDLE]
_CloseHandle.restype = wintypes.BOOL

_GetNamedSecurityInfoW = _advapi32.GetNamedSecurityInfoW
_GetNamedSecurityInfoW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.POINTER(wintypes.LPVOID),
    ctypes.POINTER(wintypes.LPVOID),
]
_GetNamedSecurityInfoW.restype = wintypes.DWORD

_GetSecurityDescriptorSacl = _advapi32.GetSecurityDescriptorSacl
_GetSecurityDescriptorSacl.argtypes = [
    wintypes.LPVOID,
    ctypes.POINTER(wintypes.BOOL),
    ctypes.POINTER(wintypes.LPVOID),
    ctypes.POINTER(wintypes.BOOL),
]
_GetSecurityDescriptorSacl.restype = wintypes.BOOL

_GetAce = _advapi32.GetAce
_GetAce.argtypes = [
    ctypes.POINTER(_ACL),
    wintypes.DWORD,
    ctypes.POINTER(wintypes.LPVOID),
]
_GetAce.restype = wintypes.BOOL

_LocalFree = _kernel32.LocalFree
_LocalFree.argtypes = [wintypes.LPVOID]
_LocalFree.restype = wintypes.LPVOID


def level_name(level):
    # type: (object) -> str
    """Return the Windows name for an integrity level number."""
    try:
        number = int(level)
    except (TypeError, ValueError):
        return "unknown"
    return _LEVEL_NAMES.get(number, "0x%X" % number)


def _sid_level(sid):
    # type: (object) -> object
    if not sid:
        return None
    try:
        body = ctypes.cast(sid, ctypes.POINTER(_SID)).contents
        count = int(body.SubAuthorityCount)
        if count < 1:
            return None
        return int(body.SubAuthority[count - 1])
    except (TypeError, ValueError):
        return None


def _open_token():
    token = wintypes.HANDLE()
    if not _OpenProcessToken(
        _GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)
    ):
        return None
    return token


def _token_buffer(token, info_class):
    size = wintypes.DWORD(0)
    _GetTokenInformation(token, info_class, None, 0, ctypes.byref(size))
    if not size.value:
        return None
    buffer = ctypes.create_string_buffer(size.value)
    if not _GetTokenInformation(
        token, info_class, buffer, size, ctypes.byref(size)
    ):
        return None
    return buffer


def process_integrity_level():
    # type: () -> object
    """Return this process token's integrity level number, or ``None``."""
    try:
        token = _open_token()
    except Exception:
        return None
    if not token:
        return None
    try:
        buffer = _token_buffer(token, _TOKEN_INTEGRITY_LEVEL)
        if buffer is None:
            return None
        label = ctypes.cast(
            buffer, ctypes.POINTER(_TOKEN_MANDATORY_LABEL)
        ).contents
        return _sid_level(label.Label.Sid)
    except Exception:
        return None
    finally:
        _CloseHandle(token)


def _no_write_up():
    # type: () -> bool
    """Whether this token is subject to mandatory no-write-up (the default)."""
    try:
        token = _open_token()
    except Exception:
        return True
    if not token:
        return True
    try:
        buffer = _token_buffer(token, _TOKEN_MANDATORY_POLICY)
        if buffer is None:
            return True
        policy = ctypes.cast(
            buffer, ctypes.POINTER(wintypes.DWORD)
        ).contents.value
        return bool(policy & _NO_WRITE_UP)
    except Exception:
        return True
    finally:
        _CloseHandle(token)


def _native_path(path):
    # type: (object) -> str
    if not path:
        return ""
    text = os.path.abspath(os.path.normpath(str(path)))
    # GetNamedSecurityInfoW rejects a trailing separator on a non-root path.
    if len(text) > 3 and text.endswith(os.sep):
        text = text.rstrip(os.sep)
    return text


def explicit_object_level(path):
    # type: (str) -> object
    """Return the label stored on ``path`` itself, or ``None`` when unlabelled.

    Only ``LABEL_SECURITY_INFORMATION`` is requested, which does not require
    ``SeSecurityPrivilege``.
    """
    name = _native_path(path)
    if not name:
        return None
    sacl = wintypes.LPVOID()
    descriptor = wintypes.LPVOID()
    try:
        result = _GetNamedSecurityInfoW(
            name,
            _SE_FILE_OBJECT,
            _LABEL_SECURITY_INFORMATION,
            None,
            None,
            None,
            ctypes.byref(sacl),
            ctypes.byref(descriptor),
        )
    except Exception:
        return None
    if result != 0 or not descriptor:
        return None
    try:
        present = wintypes.BOOL(0)
        defaulted = wintypes.BOOL(0)
        if not _GetSecurityDescriptorSacl(
            descriptor,
            ctypes.byref(present),
            ctypes.byref(sacl),
            ctypes.byref(defaulted),
        ):
            return None
        if not present.value or not sacl:
            return None
        acl = ctypes.cast(sacl, ctypes.POINTER(_ACL))
        for index in range(int(acl.contents.AceCount)):
            ace = wintypes.LPVOID()
            if not _GetAce(acl, index, ctypes.byref(ace)) or not ace:
                continue
            header = ctypes.cast(
                ace, ctypes.POINTER(_ACE_HEADER)
            ).contents
            if header.AceType != _SYSTEM_MANDATORY_LABEL_ACE_TYPE:
                continue
            address = ace.value + _SYSTEM_MANDATORY_LABEL_ACE.SidStart.offset
            return _sid_level(ctypes.c_void_p(address))
        return None
    except Exception:
        return None
    finally:
        _LocalFree(descriptor)


def object_integrity_level(path):
    # type: (str) -> object
    """Return the effective label of ``path``.

    A label is inherited only through the label ACE's inherit flags at creation
    time, so an object without a stored label is its own authority and Windows
    treats it as Medium - it does not borrow a labelled ancestor's level (the
    root of an administrative volume is often labelled High while its existing
    children are not).  ``None`` is returned when the query is unavailable.
    """
    level = explicit_object_level(path)
    if level is None:
        return MEDIUM
    return level


def integrity_denial(path):
    # type: (str) -> object
    """Return ``(process_level, object_level)`` when no-write-up blocks ``path``.

    ``None`` means writes are not blocked by integrity, or the diagnosis is
    unavailable.
    """
    try:
        if not _no_write_up():
            return None
        process_level = process_integrity_level()
        if process_level is None:
            return None
        object_level = object_integrity_level(path)
        if object_level is None or process_level >= object_level:
            return None
        return process_level, object_level
    except Exception:
        return None


def describe_denial(path):
    # type: (str) -> str
    """Explain an integrity denial of ``path`` as a message suffix, or ``""``."""
    levels = integrity_denial(path)
    if levels is None:
        return ""
    process_level, object_level = levels
    folder = os.path.dirname(_native_path(path)) or _native_path(path)
    if getattr(sys, "frozen", False):
        remedy = (
            '; close DiskWarden and give its folder a Medium label, then start'
            ' it again: icacls "%s" /setintegritylevel (OI)(CI)M /T'
            % os.path.dirname(os.path.abspath(sys.executable))
        )
    else:
        remedy = (
           "; restart DiskWarden from an ordinary (Medium) window instead of a"
            " low-integrity one"
        )
    return (
        "; the app runs at integrity level %s but %s requires %s, and Windows"
        " mandatory no-write-up forbids a lower-integrity process from"
        " renaming, moving, creating folders in or deleting anything there"
        " (this is not an open file handle)%s"
        % (
            level_name(process_level),
            folder,
            level_name(object_level),
            remedy,
        )
    )


__all__ = [
    "HIGH",
    "LOW",
    "MEDIUM",
    "SYSTEM",
    "UNTRUSTED",
    "describe_denial",
    "explicit_object_level",
    "integrity_denial",
    "level_name",
    "object_integrity_level",
    "process_integrity_level",
]
