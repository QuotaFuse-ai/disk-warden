# -*- coding: utf-8 -*-
"""macOS links and volume identity.

macOS has no junction: a directory symlink provides the same "this path now
lives over there" behaviour, and ``ln -s`` semantics are what the migration
layer needs.  The adapter keeps the Windows contract:

* a created link is always read back and compared with the requested target;
  a link that does not resolve to the target is removed and reported as a
  :class:`LinkError` (acceptance: "符号链接建链后读回目标正确");
* removing a link uses ``os.unlink`` on the link itself, never a recursive
  delete, so the target directory is never entered or touched;
* volume identity comes from ``statfs(2)`` (mount point and filesystem name)
  plus ``st_dev`` as the device identity.  The POSIX fallback that used
  ``/`` for every mount point made ``same_volume`` claim two different disks
  were the same, which would let the swap step rename across volumes.

Python 3.8 compatible (decision 0005).  ``ctypes.CDLL(None)`` is only called
on macOS; importing this module elsewhere is safe (tests do that).
"""

import ctypes
import errno
import os
import stat as stat_module
import sys

from ..link import LinkError, VolumeInfo, link_target_matches, strip_extended_prefix

# Filesystems that can store symlinks.  exFAT/FAT (typical USB sticks) cannot;
# a link there fails with EPERM/EOPNOTSUPP at creation time, so the precheck
# reports the requirement before any data is copied.
LINK_FILESYSTEMS = ("apfs", "hfs")

_MNT_NAME_BYTES = 1024
_FS_NAME_BYTES = 16


class _Statfs(ctypes.Structure):
    """``struct statfs`` as declared in macOS ``<sys/mount.h>`` (LP64)."""

    _fields_ = [
        ("f_bsize", ctypes.c_uint32),
        ("f_iosize", ctypes.c_int32),
        ("f_blocks", ctypes.c_uint64),
        ("f_bfree", ctypes.c_uint64),
        ("f_bavail", ctypes.c_uint64),
        ("f_files", ctypes.c_uint64),
        ("f_ffree", ctypes.c_uint64),
        ("f_fsid", ctypes.c_int32 * 2),
        ("f_owner", ctypes.c_uint32),
        ("f_type", ctypes.c_uint32),
        ("f_flags", ctypes.c_uint32),
        ("f_fssubtype", ctypes.c_uint32),
        ("f_fstypename", ctypes.c_char * _FS_NAME_BYTES),
        ("f_mntonname", ctypes.c_char * _MNT_NAME_BYTES),
        ("f_mntfromname", ctypes.c_char * _MNT_NAME_BYTES),
        ("f_reserved", ctypes.c_uint32 * 8),
    ]


def _probe(path):
    # type: (str) -> _Statfs
    """Call ``statfs(2)``; only available on macOS."""
    if sys.platform != "darwin":
        raise RuntimeError("statfs(2) is only available on macOS")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.statfs.argtypes = [ctypes.c_char_p, ctypes.POINTER(_Statfs)]
    libc.statfs.restype = ctypes.c_int
    buffer = _Statfs()
    if libc.statfs(os.fsencode(path), ctypes.byref(buffer)) != 0:
        error = ctypes.get_errno() or errno.EIO
        raise OSError(error, os.strerror(error), path)
    return buffer


def _decode(value):
    # type: (bytes) -> str
    return os.fsdecode(value.split(b"\0", 1)[0])


def _mount_point(path):
    # type: (str) -> str
    """Longest existing ancestor that the OS reports as a mount point."""
    current = os.path.realpath(path)
    while True:
        try:
            if os.path.ismount(current):
                return current
        except OSError:  # pragma: no cover - defensive
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return current
        current = parent


def volume_info(path):
    # type: (str) -> VolumeInfo
    """Return mount point, filesystem name and device id of ``path``'s volume.

    ``serial`` is ``st_dev``: it identifies the mounted volume for the whole
    boot and is the value ``same_volume`` compares.  It is not the disk's
    hardware serial, which the product does not need.
    """
    absolute = os.path.abspath(os.path.normpath(strip_extended_prefix(path)))
    try:
        info = os.lstat(absolute)
    except OSError as exc:
        raise LinkError("cannot inspect %s: %s" % (absolute, exc))

    mount_point = ""
    fs_name = ""
    try:
        probe = _probe(absolute)
    except (OSError, RuntimeError, AttributeError):
        probe = None
    if probe is not None:
        mount_point = _decode(probe.f_mntonname)
        fs_name = _decode(probe.f_fstypename)
    if not mount_point:
        mount_point = _mount_point(absolute)

    return VolumeInfo(
        mount_point=mount_point,
        fs_name=fs_name,
        serial=int(info.st_dev),
    )


def link_support(path):
    # type: (str) -> dict
    """Describe whether ``path``'s volume can hold symlinks.

    An unknown filesystem is reported as supported: creation then fails with a
    precise ``LinkError`` instead of a guess blocking a valid disk.
    """
    info = volume_info(path)
    fs_name = info.fs_name or ""
    filesystem = fs_name.lower()
    supported = (not filesystem) or filesystem in LINK_FILESYSTEMS
    return {
        "supported": supported,
        "code": "TARGET_FS_UNSUPPORTED",
        "message": "the target disk must be APFS or HFS+ to create links",
        "fs_name": fs_name,
        "mount_point": info.mount_point,
    }


def is_junction(path):
    # type: (str) -> bool
    """True for a symlink that resolves to a directory."""
    try:
        return os.path.islink(path) and os.path.isdir(path)
    except OSError:
        return False


def is_link(path):
    # type: (str) -> bool
    try:
        return os.path.islink(path)
    except OSError:
        return False


def create_junction(link_path, target_path):
    # type: (str, str) -> str
    """Create ``link_path`` as a symlink to ``target_path`` and verify it.

    Never replaces an existing path.  The directory target must exist.  A
    link whose read-back target differs is unlinked (the link only, never the
    target) and reported as a :class:`LinkError`.
    """
    link_path = os.path.abspath(os.path.normpath(strip_extended_prefix(link_path)))
    target_path = os.path.abspath(
        os.path.normpath(strip_extended_prefix(target_path))
    )
    if not os.path.isdir(target_path):
        raise LinkError("link target is not a directory: %s" % target_path)
    if os.path.lexists(link_path):
        raise LinkError("link path already exists: %s" % link_path)
    if not os.path.isdir(os.path.dirname(link_path)):
        raise LinkError(
            "link parent does not exist: %s" % os.path.dirname(link_path)
        )

    try:
        os.symlink(target_path, link_path, target_is_directory=True)
        actual = read_junction_target(link_path)
        if not link_target_matches(actual, target_path):
            raise LinkError(
                "symlink %s resolved to %r instead of %r"
                % (link_path, actual, target_path)
            )
    except (LinkError, OSError) as exc:
        _discard_failed_link(link_path)
        if isinstance(exc, LinkError):
            raise
        raise LinkError("symlink failed: %s" % exc)
    return link_path


def _discard_failed_link(link_path):
    # type: (str) -> None
    """Remove the half-created symlink; never follows it, never raises."""
    try:
        if os.path.islink(link_path):
            os.unlink(link_path)
    except OSError:  # pragma: no cover - best effort
        pass


def read_junction_target(path):
    # type: (str) -> str
    """Return the symlink target, or ``None`` when ``path`` is no link."""
    try:
        target = os.readlink(path)
    except OSError:
        return None
    return strip_extended_prefix(target)


def remove_junction(path, expected_target=None):
    # type: (str, str) -> bool
    """Unlink the symlink at ``path``; the target is never entered.

    Refuses non-links outright and, when ``expected_target`` is given, refuses
    a link that points somewhere else.  Returns False when already absent.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise LinkError("cannot inspect link %s: %s" % (path, exc))

    if not stat_module.S_ISLNK(info.st_mode):
        raise LinkError("refusing to remove a non-link path: %s" % path)

    if expected_target is not None:
        actual = read_junction_target(path)
        if not link_target_matches(actual, expected_target):
            raise LinkError(
                "link %s points to %r, expected %r"
                % (path, actual, expected_target)
            )

    os.unlink(path)
    return True


def rename_dir(source, destination):
    # type: (str, str) -> str
    """Atomic same-volume rename; refuses to replace an existing path."""
    if os.path.lexists(destination):
        raise LinkError("rename destination already exists: %s" % destination)
    os.rename(source, destination)
    return destination


__all__ = [
    "LINK_FILESYSTEMS",
    "create_junction",
    "is_junction",
    "is_link",
    "link_support",
    "read_junction_target",
    "remove_junction",
    "rename_dir",
    "volume_info",
]
