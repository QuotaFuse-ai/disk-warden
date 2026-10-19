# -*- coding: utf-8 -*-
"""Windows junctions and volume identity.

Junctions are created with ``mklink /J`` (no elevation required) and removed
with ``os.rmdir``, which unlinks the reparse point without entering the
target.  Target read-back uses ``os.readlink`` and strips the ``\\\\?\\``
prefix; the result may be an 8.3 short path, so comparison belongs to
:func:`src.platform.link.link_target_matches`.

Python 3.8 compatible (decision 0005).
"""

import ctypes
import os
import stat
import subprocess
from ctypes import wintypes

from ..filesystem import FILE_ATTRIBUTE_REPARSE_POINT
from ..link import (
    LinkError,
    VolumeInfo,
    link_target_matches,
    strip_extended_prefix,
)

IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_MAX_PATH = 261

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_GetVolumePathNameW = _kernel32.GetVolumePathNameW
_GetVolumePathNameW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.LPWSTR,
    wintypes.DWORD,
]
_GetVolumePathNameW.restype = wintypes.BOOL

_GetVolumeInformationW = _kernel32.GetVolumeInformationW
_GetVolumeInformationW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.LPWSTR,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD),
    wintypes.LPWSTR,
    wintypes.DWORD,
]
_GetVolumeInformationW.restype = wintypes.BOOL


def _reparse_tag(info):
    # type: (os.stat_result) -> int
    return int(getattr(info, "st_reparse_tag", 0) or 0)


def _is_reparse(info):
    # type: (os.stat_result) -> bool
    attributes = int(getattr(info, "st_file_attributes", 0) or 0)
    return bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def is_junction(path):
    # type: (str) -> bool
    """True only for a directory junction (not a symlink)."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if not _is_reparse(info):
        return False
    tag = _reparse_tag(info)
    # ``st_reparse_tag`` exists on Windows in Python 3.8; when it is missing
    # (older runtimes) a directory reparse point is the best evidence we have.
    if tag:
        return tag == IO_REPARSE_TAG_MOUNT_POINT
    return stat.S_ISDIR(info.st_mode)


def is_link(path):
    # type: (str) -> bool
    """True for a junction or any other directory/file reparse point."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return _is_reparse(info)


def create_junction(link_path, target_path):
    # type: (str, str) -> str
    """Create ``link_path`` as a junction to ``target_path``.

    Never replaces an existing path: a link already present is reported as an
    error so the caller can inspect it first (architecture 6.5).
    """
    link_path = os.path.abspath(os.path.normpath(link_path))
    target_path = os.path.abspath(os.path.normpath(target_path))
    if not os.path.isdir(target_path):
        raise LinkError("junction target is not a directory: %s" % target_path)
    if os.path.lexists(link_path):
        raise LinkError("link path already exists: %s" % link_path)
    if not os.path.isdir(os.path.dirname(link_path)):
        raise LinkError(
            "link parent does not exist: %s" % os.path.dirname(link_path)
        )

    try:
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", link_path, target_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=_CREATE_NO_WINDOW,
        )
    except OSError as exc:
        raise LinkError("mklink failed to start: %s" % exc)

    if completed.returncode != 0:
        output = completed.stdout.decode("mbcs", "replace").strip()
        raise LinkError(
            "mklink exited with %d%s"
            % (
                completed.returncode,
                ": %s" % output if output else "",
            )
        )
    return link_path


def read_junction_target(path):
    # type: (str) -> str
    """Return the junction target without the extended-length prefix.

    Returns ``None`` when ``path`` is not a link or cannot be read.
    """
    try:
        target = os.readlink(path)
    except OSError:
        return None
    return strip_extended_prefix(target)


def remove_junction(path, expected_target=None):
    # type: (str, str) -> bool
    """Remove a link without touching its target.

    Refuses non-links outright and, when ``expected_target`` is given, refuses
    a link that points somewhere else.  Returns False when the path is already
    absent (idempotent delete, architecture 6.5).
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise LinkError("cannot inspect link %s: %s" % (path, exc))

    if not _is_reparse(info):
        raise LinkError("refusing to remove a non-link path: %s" % path)

    if expected_target is not None:
        actual = read_junction_target(path)
        if not link_target_matches(actual, expected_target):
            raise LinkError(
                "link %s points to %r, expected %r"
                % (path, actual, expected_target)
            )

    if stat.S_ISDIR(info.st_mode):
        os.rmdir(path)
    else:
        os.unlink(path)
    return True


def rename_dir(source, destination):
    # type: (str, str) -> str
    """Atomic same-volume rename; refuses to replace an existing path."""
    if os.path.lexists(destination):
        raise LinkError("rename destination already exists: %s" % destination)
    os.rename(source, destination)
    return destination


def volume_info(path):
    # type: (str) -> VolumeInfo
    """Return the mount point, filesystem name and serial of ``path``'s volume."""
    absolute = os.path.abspath(os.path.normpath(path))
    root_buffer = ctypes.create_unicode_buffer(_MAX_PATH)
    if not _GetVolumePathNameW(absolute, root_buffer, _MAX_PATH):
        error = ctypes.get_last_error()
        raise LinkError(
            "cannot resolve volume for %s (error %d)" % (absolute, error)
        )

    fs_buffer = ctypes.create_unicode_buffer(_MAX_PATH)
    serial = wintypes.DWORD()
    max_component = wintypes.DWORD()
    flags = wintypes.DWORD()
    if not _GetVolumeInformationW(
        root_buffer.value,
        None,
        0,
        ctypes.byref(serial),
        ctypes.byref(max_component),
        ctypes.byref(flags),
        fs_buffer,
        _MAX_PATH,
    ):
        error = ctypes.get_last_error()
        raise LinkError(
            "cannot read volume information for %s (error %d)"
            % (root_buffer.value, error)
        )

    return VolumeInfo(
        mount_point=root_buffer.value,
        fs_name=fs_buffer.value,
        serial=int(serial.value),
    )


__all__ = [
    "IO_REPARSE_TAG_MOUNT_POINT",
    "create_junction",
    "is_junction",
    "is_link",
    "read_junction_target",
    "remove_junction",
    "rename_dir",
    "volume_info",
]
