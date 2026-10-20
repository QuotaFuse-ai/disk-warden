# -*- coding: utf-8 -*-
"""Windows open-handle diagnostics (Restart Manager).

Renaming a directory fails with ``ERROR_ACCESS_DENIED`` when any file below it
is open without ``FILE_SHARE_DELETE``.  The error alone does not say who holds
the handle, so this module asks the Restart Manager which processes have the
directory or a sample of its files open.  The answer is diagnostic only: the
caller still owns the decision to block or retry.

Registration is bounded (a sample of files, never the whole tree) and every
failure is swallowed: a scan or migration must never fail because the
diagnostic cannot run.

Python 3.8 compatible (decision 0005).
"""

import ctypes
import os
import stat as stat_module
from ctypes import wintypes

from ..process import ProcessInfo

CCH_RM_SESSION_KEY = 32
CCH_RM_MAX_APP_NAME = 255
CCH_RM_MAX_SVC_NAME = 63

MAX_HOLDERS = 8
REGISTERED_PATHS = 64

_ERROR_SUCCESS = 0
_ERROR_MORE_DATA = 234

_rm = ctypes.WinDLL("rstrtmgr", use_last_error=True)


class _RM_UNIQUE_PROCESS(ctypes.Structure):
    _fields_ = [
        ("dwProcessId", wintypes.DWORD),
        ("ProcessStartTime", wintypes.FILETIME),
    ]


class _RM_PROCESS_INFO(ctypes.Structure):
    _fields_ = [
        ("Process", _RM_UNIQUE_PROCESS),
        ("strAppName", wintypes.WCHAR * (CCH_RM_MAX_APP_NAME + 1)),
        ("strServiceShortName", wintypes.WCHAR * (CCH_RM_MAX_SVC_NAME + 1)),
        ("ApplicationType", ctypes.c_int),
        ("AppStatus", wintypes.ULONG),
        ("TSSessionId", wintypes.DWORD),
        ("bRestartable", wintypes.BOOL),
    ]


_RmStartSession = _rm.RmStartSession
_RmStartSession.argtypes = [
    ctypes.POINTER(wintypes.DWORD),
    wintypes.DWORD,
    wintypes.LPWSTR,
]
_RmStartSession.restype = wintypes.DWORD

_RmRegisterResources = _rm.RmRegisterResources
_RmRegisterResources.argtypes = [
    wintypes.DWORD,
    wintypes.UINT,
    ctypes.POINTER(wintypes.LPCWSTR),
    wintypes.UINT,
    ctypes.POINTER(_RM_UNIQUE_PROCESS),
    wintypes.UINT,
    ctypes.POINTER(wintypes.LPCWSTR),
]
_RmRegisterResources.restype = wintypes.DWORD

_RmGetList = _rm.RmGetList
_RmGetList.argtypes = [
    wintypes.DWORD,
    ctypes.POINTER(wintypes.UINT),
    ctypes.POINTER(wintypes.UINT),
    ctypes.POINTER(_RM_PROCESS_INFO),
    ctypes.POINTER(wintypes.DWORD),
]
_RmGetList.restype = wintypes.DWORD

_RmEndSession = _rm.RmEndSession
_RmEndSession.argtypes = [wintypes.DWORD]
_RmEndSession.restype = wintypes.DWORD


def _sample_paths(path, limit):
    # type: (str, int) -> list
    """Return up to ``limit`` regular files below ``path``, shallowest first.

    Restart Manager accepts files only; a registered directory is rejected
    with ``ERROR_ACCESS_DENIED``.  Nothing is opened here, so the sample
    cannot itself hold a handle that would then be reported.
    """
    collected = []  # type: list
    pending = [path]
    budget = max(1, int(limit))
    while pending and len(collected) < budget:
        directory = pending.pop(0)
        try:
            with os.scandir(directory) as entries:
                children = list(entries)
        except OSError:
            continue
        for entry in children:
            if len(collected) >= budget:
                break
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat_module.S_ISDIR(info.st_mode):
                pending.append(entry.path)
            elif stat_module.S_ISREG(info.st_mode):
                collected.append(entry.path)
    return collected


def _register(session, paths):
    # type: (int, list) -> int
    array = (wintypes.LPCWSTR * len(paths))(*paths)
    return int(
        _RmRegisterResources(
            wintypes.DWORD(session),
            wintypes.UINT(len(paths)),
            array,
            wintypes.UINT(0),
            None,
            wintypes.UINT(0),
            None,
        )
    )


def _query(session, capacity):
    # type: (int, int) -> tuple
    needed = wintypes.UINT(0)
    written = wintypes.UINT(capacity)
    reasons = wintypes.DWORD(0)
    buffer = (_RM_PROCESS_INFO * capacity)()
    result = int(
        _RmGetList(
            wintypes.DWORD(session),
            ctypes.byref(needed),
            ctypes.byref(written),
            buffer,
            ctypes.byref(reasons),
        )
    )
    return result, needed.value, written.value, buffer


def find_dir_holders(path):
    # type: (str) -> list
    """Return ``ProcessInfo`` entries holding ``path`` or its sampled files.

    Returns an empty list on non-Windows callers, when nothing is open, or
    whenever the Restart Manager cannot answer.  Never raises.
    """
    try:
        return _find_dir_holders(path)
    except Exception:
        return []


def _find_dir_holders(path):
    # type: (str) -> list
    absolute = os.path.abspath(os.path.normpath(str(path)))
    if not os.path.isdir(absolute):
        return []
    paths = _sample_paths(absolute, REGISTERED_PATHS)
    if not paths:
        return []

    session = wintypes.DWORD(0)
    key = ctypes.create_unicode_buffer(CCH_RM_SESSION_KEY + 1)
    if _RmStartSession(ctypes.byref(session), 0, key) != _ERROR_SUCCESS:
        return []
    try:
        if _register(session.value, paths) != _ERROR_SUCCESS:
            return []

        result, needed, written, buffer = _query(session.value, MAX_HOLDERS)
        if result == _ERROR_MORE_DATA:
            capacity = min(max(needed, MAX_HOLDERS), MAX_HOLDERS * 4)
            result, needed, written, buffer = _query(session.value, capacity)
        if result != _ERROR_SUCCESS:
            return []
        return _dedupe(buffer, written)
    finally:
        _RmEndSession(session.value)


def _dedupe(buffer, written):
    # type: (object, int) -> list
    holders = []
    seen = set()
    own_pid = os.getpid()
    for index in range(written):
        info = buffer[index]
        pid = int(info.Process.dwProcessId)
        if not pid or pid == own_pid or pid in seen:
            continue
        name = str(info.strAppName or "").strip()
        if not name:
            name = str(info.strServiceShortName or "").strip()
        seen.add(pid)
        holders.append(ProcessInfo(pid=pid, name=name or "unknown"))
        if len(holders) >= MAX_HOLDERS:
            break
    return holders


__all__ = ["MAX_HOLDERS", "REGISTERED_PATHS", "find_dir_holders"]
