# -*- coding: utf-8 -*-
"""Atomic JSON persistence primitives (architecture 4.5 / AC-STORE-02).

Every write goes to ``<target>.tmp-<pid>`` in the same directory, is flushed
and fsynced, and only then replaces the target with ``os.replace``.  A reader
therefore sees either the previous complete file or the new complete file,
never a half-written one.  Corrupt files are quarantined (renamed aside)
instead of being silently overwritten, so a damaged file can be inspected.

Python 3.8 compatible (decision 0005).
"""

import codecs
import datetime
import json
import os

TEMP_MARKER = ".tmp-"
CORRUPT_MARKER = ".corrupt-"


class StoreError(RuntimeError):
    """Raised when a store write cannot be completed."""


class StoreCorruptError(StoreError):
    """Raised when a JSON file exists but cannot be parsed."""


def utc_now(now=None):
    # type: (object) -> str
    """ISO-8601 UTC timestamp with a trailing ``Z`` (architecture 4.5)."""
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return (
        moment.astimezone(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def temp_path(path):
    # type: (str) -> str
    return "%s%s%d" % (path, TEMP_MARKER, os.getpid())


def is_temp_name(name):
    # type: (str) -> bool
    return TEMP_MARKER in (name or "")


def _fsync_directory(directory):
    # type: (str) -> None
    """Best-effort directory fsync; Windows cannot open directories."""
    if os.name == "nt":
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def atomic_write_bytes(path, payload, fsync=True):
    # type: (str, bytes, bool) -> str
    """Atomically replace ``path`` with ``payload``; returns the absolute path."""
    target = os.path.abspath(os.path.normpath(path))
    directory = os.path.dirname(target)
    try:
        if directory:
            os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        raise StoreError("cannot create %s: %s" % (directory, exc))

    temporary = temp_path(target)
    try:
        with open(temporary, "wb") as handle:
            handle.write(payload)
            handle.flush()
            if fsync:
                os.fsync(handle.fileno())
        os.replace(temporary, target)
        _fsync_directory(directory)
    except OSError as exc:
        try:
            if os.path.exists(temporary):
                os.unlink(temporary)
        except OSError:
            pass
        raise StoreError("cannot write %s atomically: %s" % (target, exc))
    return target


def read_json_file(path):
    # type: (str) -> object
    """Read UTF-8 JSON without a BOM; raises ``StoreCorruptError`` if invalid."""
    with open(path, "rb") as handle:
        raw = handle.read()
    if raw.startswith(codecs.BOM_UTF8):
        raw = raw[len(codecs.BOM_UTF8):]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StoreCorruptError("invalid UTF-8 in %s: %s" % (path, exc))
    if not text.strip():
        raise StoreCorruptError("empty JSON file: %s" % path)
    try:
        return json.loads(text)
    except ValueError as exc:
        raise StoreCorruptError("invalid JSON in %s: %s" % (path, exc))


def dump_json_bytes(data, indent=2):
    # type: (object, int) -> bytes
    try:
        text = json.dumps(data, ensure_ascii=False, indent=indent, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise StoreError("cannot serialise JSON: %s" % exc)
    return text.encode("utf-8") + b"\n"


def atomic_write_json(path, data, backup=False, indent=2):
    # type: (str, object, bool, int) -> str
    """Atomically write JSON.  ``backup`` keeps the previous valid file as .bak."""
    payload = dump_json_bytes(data, indent=indent)
    if backup:
        backup_path(path)
    return atomic_write_bytes(path, payload)


def backup_path(path):
    # type: (str) -> str
    """Copy a valid existing file to ``<path>.bak`` atomically.

    A corrupt current file never replaces an existing good ``.bak``: the
    fallback keeps its last valid content.
    """
    if not os.path.isfile(path):
        return ""
    try:
        read_json_file(path)
    except (StoreCorruptError, OSError):
        return ""
    with open(path, "rb") as handle:
        payload = handle.read()
    return atomic_write_bytes(path + ".bak", payload)


def quarantine_corrupt(path, now=None):
    # type: (str, object) -> str
    """Rename a corrupt file aside; returns the new path or an empty string."""
    stamp = (now or datetime.datetime.now(datetime.timezone.utc)).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    candidate = "%s%s%s" % (path, CORRUPT_MARKER, stamp)
    suffix = 1
    while os.path.exists(candidate):
        candidate = "%s%s%s-%d" % (path, CORRUPT_MARKER, stamp, suffix)
        suffix += 1
    try:
        os.replace(path, candidate)
    except OSError:
        return ""
    return candidate


def load_json(path, default=None, quarantine=True, now=None):
    # type: (str, object, bool, object) -> object
    """Read JSON, returning ``default`` for a missing or corrupt file.

    Corrupt files are quarantined by default so the caller can rebuild a
    default file without destroying evidence (AC-STORE-02).
    """
    try:
        return read_json_file(path)
    except FileNotFoundError:
        return default
    except StoreCorruptError:
        if quarantine:
            quarantine_corrupt(path, now=now)
        return default


__all__ = [
    "CORRUPT_MARKER",
    "StoreCorruptError",
    "StoreError",
    "TEMP_MARKER",
    "atomic_write_bytes",
    "atomic_write_json",
    "backup_path",
    "dump_json_bytes",
    "is_temp_name",
    "load_json",
    "quarantine_corrupt",
    "read_json_file",
    "temp_path",
    "utc_now",
]
