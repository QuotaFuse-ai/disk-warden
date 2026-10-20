# -*- coding: utf-8 -*-
"""Windows junctions and volume identity.

Junctions are written as reparse points directly through ``DeviceIoControl``
(T26/R-01).  ``cmd /c mklink`` is never used: ``cmd`` re-parses the rest of
the command line, so ``&``, ``|`` and ``^`` inside a path truncate the
command and silently create a junction that points at a non-existent prefix.
The direct call takes the target as a UTF-16 buffer, so no path can be
reinterpreted as shell syntax, and no elevation is required.

Every created link is read back and compared with the requested target before
success is reported; a link that does not match is removed and reported as a
:class:`LinkError`.  Links are removed with ``os.rmdir``, which unlinks the
reparse point without entering the target.  Target read-back uses
``os.readlink`` and strips the ``\\\\?\\`` prefix; the result may be an 8.3
short path, so comparison belongs to
:func:`src.platform.link.link_target_matches`.

Python 3.8 compatible (decision 0005).
"""

import ctypes
import os
import stat
import struct
from ctypes import wintypes

from ..filesystem import FILE_ATTRIBUTE_REPARSE_POINT
from ..link import (
    LinkError,
    VolumeInfo,
    link_target_matches,
    strip_extended_prefix,
)

IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003

# Win32 constants for the reparse point write path.  Values are stable ABI
# (winnt.h / winbase.h); no third-party dependency is introduced.
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FSCTL_SET_REPARSE_POINT = 0x000900A4
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

# ``REPARSE_DATA_BUFFER`` header: ReparseTag (ULONG) + ReparseDataLength
# (USHORT) + Reserved (USHORT).
_REPARSE_HEADER_BYTES = 8
# MountPointReparseBuffer prefix: four USHORT offsets (substitute name offset
# and length, print name offset and length), then PathBuffer.
_MOUNT_POINT_OFFSETS_BYTES = 8

_MAX_PATH = 261

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_CreateFileW = _kernel32.CreateFileW
_CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
_CreateFileW.restype = wintypes.HANDLE

_DeviceIoControl = _kernel32.DeviceIoControl
_DeviceIoControl.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.c_void_p,
]
_DeviceIoControl.restype = wintypes.BOOL

_CloseHandle = _kernel32.CloseHandle
_CloseHandle.argtypes = [wintypes.HANDLE]
_CloseHandle.restype = wintypes.BOOL

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


def _mount_point_buffer(target_path):
    # type: (str) -> ctypes.Array
    """Build a ``REPARSE_DATA_BUFFER`` describing a junction to ``target_path``.

    The substitute name (``\\??\\<path>``) is what the filesystem follows; the
    print name is what Explorer shows.  Both are UTF-16LE byte buffers, so
    ``&``, ``|``, ``^``, ``%`` and friends carry no special meaning here.
    """
    substitute = ("\\??\\" + target_path).encode("utf-16-le")
    print_name = target_path.encode("utf-16-le")
    # Both names are NUL-terminated inside PathBuffer, but the recorded
    # lengths exclude their terminator; the print name therefore starts two
    # bytes after the substitute name.  This matches the bytes Windows itself
    # returns for ``mklink /J`` (FSCTL_GET_REPARSE_POINT).
    substitute_offset = 0
    print_offset = len(substitute) + 2
    data_length = (
        _MOUNT_POINT_OFFSETS_BYTES
        + len(substitute) + 2
        + len(print_name) + 2
    )
    total = _REPARSE_HEADER_BYTES + data_length
    buffer = ctypes.create_string_buffer(total)
    struct.pack_into(
        "<IHH", buffer, 0, IO_REPARSE_TAG_MOUNT_POINT, data_length, 0
    )
    struct.pack_into(
        "<HHHH",
        buffer,
        _REPARSE_HEADER_BYTES,
        substitute_offset,
        len(substitute),
        print_offset,
        len(print_name),
    )
    start = _REPARSE_HEADER_BYTES + _MOUNT_POINT_OFFSETS_BYTES
    buffer[start:start + len(substitute)] = substitute
    start = _REPARSE_HEADER_BYTES + _MOUNT_POINT_OFFSETS_BYTES + print_offset
    buffer[start:start + len(print_name)] = print_name
    return buffer


def _set_reparse_point(link_path, target_path):
    # type: (str, str) -> None
    """Create the junction at ``link_path``; raise :class:`LinkError` on failure.

    The empty directory is created first (``CreateFileW`` alone cannot create
    a directory), then reopened with ``FILE_FLAG_OPEN_REPARSE_POINT`` so the
    reparse point is written on the directory itself instead of its target.
    """
    buffer = _mount_point_buffer(target_path)
    extended_link = (
        link_path
        if link_path.startswith("\\\\?\\")
        else "\\\\?\\" + link_path
    )
    try:
        os.mkdir(link_path)
    except OSError as exc:
        raise LinkError(
            "cannot create link path %s: %s" % (link_path, exc)
        )
    handle = _CreateFileW(
        extended_link,
        _GENERIC_WRITE,
        0,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if not handle or handle == _INVALID_HANDLE_VALUE:
        error = ctypes.get_last_error()
        raise LinkError(
            "cannot open link path for writing: %s (error %d)"
            % (link_path, error)
        )
    try:
        returned = wintypes.DWORD(0)
        if not _DeviceIoControl(
            handle,
            _FSCTL_SET_REPARSE_POINT,
            buffer,
            ctypes.sizeof(buffer),
            None,
            0,
            ctypes.byref(returned),
            None,
        ):
            error = ctypes.get_last_error()
            raise LinkError(
                "cannot write junction %s -> %s (error %d)"
                % (link_path, target_path, error)
            )
    finally:
        _CloseHandle(handle)


def _discard_failed_link(link_path):
    # type: (str) -> None
    """Remove the half-created link; never raises.

    ``os.rmdir`` unlinks a reparse point without entering it, and also
    removes the empty directory ``CreateFileW`` may have left behind.
    """
    try:
        os.rmdir(link_path)
    except OSError:
        pass


def create_junction(link_path, target_path):
    # type: (str, str) -> str
    """Create ``link_path`` as a junction to ``target_path``.

    The reparse point is written directly (never through ``cmd``), then read
    back: a junction that does not resolve to ``target_path`` is removed and
    reported as an error, so this function never returns a link that points
    somewhere else.

    Never replaces an existing path: a link already present is reported as an
    error so the caller can inspect it first (architecture 6.5).
    """
    link_path = os.path.abspath(os.path.normpath(strip_extended_prefix(link_path)))
    target_path = os.path.abspath(
        os.path.normpath(strip_extended_prefix(target_path))
    )
    if not os.path.isdir(target_path):
        raise LinkError("junction target is not a directory: %s" % target_path)
    if os.path.lexists(link_path):
        raise LinkError("link path already exists: %s" % link_path)
    if not os.path.isdir(os.path.dirname(link_path)):
        raise LinkError(
            "link parent does not exist: %s" % os.path.dirname(link_path)
        )

    try:
        _set_reparse_point(link_path, target_path)
        actual = read_junction_target(link_path)
        if not link_target_matches(actual, target_path):
            raise LinkError(
                "junction %s resolved to %r instead of %r"
                % (link_path, actual, target_path)
            )
    except LinkError:
        _discard_failed_link(link_path)
        raise
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
