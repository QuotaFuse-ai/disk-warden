# -*- coding: utf-8 -*-
"""Configuration backup (S1) with a SHA-256 manifest.

Only small, well-known configuration files are copied.  Data directories are
never traversed below ``MAX_CONFIG_DEPTH`` and a size cap keeps a rogue JSON
file from turning the backup into a data copy.  The manifest is written last,
so its presence proves the backup completed.

Python 3.8 compatible (decision 0005).
"""

import datetime
import hashlib
import json
import os
import stat as stat_module

from ...platform import is_reparse_stat
from . import paths
from .models import BackupEntry, BackupManifest

CONFIG_FILENAMES = frozenset(
    [
        "settings.json",
        "settings.json.backup",
        "keybindings.json",
        "argv.json",
        "storage.json",
        "extensions.json",
    ]
)

MAX_CONFIG_FILE_BYTES = 256 * 1024
MAX_CONFIG_DEPTH = 3
MANIFEST_NAME = "manifest.json"
FILES_DIR_NAME = "files"
CHUNK_SIZE = 64 * 1024


class BackupError(RuntimeError):
    """Raised when a config file cannot be backed up; migration must abort."""


def _relative_to_root(root, path):
    # type: (str, str) -> str
    rel = os.path.relpath(path, root)
    normalised = rel.replace("\\", "/")
    if normalised.startswith("../") or normalised == "..":
        return os.path.basename(path)
    return normalised


def _discover_in_directory(root, max_depth, max_file_bytes):
    # type: (str, int, int) -> tuple
    selected = []  # type: list
    skipped = []  # type: list
    stack = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            with os.scandir(directory) as entries:
                children = list(entries)
        except OSError as exc:
            raise BackupError("cannot list %s: %s" % (directory, exc))
        for entry in children:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise BackupError("cannot stat %s: %s" % (entry.path, exc))
            rel = _relative_to_root(root, entry.path)
            if is_reparse_stat(info):
                if entry.name in CONFIG_FILENAMES:
                    skipped.append({"path": rel, "reason": "reparse_point"})
                continue
            if stat_module.S_ISDIR(info.st_mode):
                if depth + 1 < max_depth:
                    stack.append((entry.path, depth + 1))
                continue
            if entry.name not in CONFIG_FILENAMES:
                continue
            if info.st_size > max_file_bytes:
                skipped.append({"path": rel, "reason": "too_large"})
                continue
            selected.append(entry.path)
    selected.sort(key=lambda item: item.lower())
    return selected, skipped


def discover_config_files(
    source_path, max_depth=MAX_CONFIG_DEPTH, max_file_bytes=MAX_CONFIG_FILE_BYTES
):
    # type: (str, int, int) -> tuple
    """Return ``(selected, skipped)`` for known config files below a source.

    A source that is itself a config file selects that file.  Reparse points
    are never followed; matched config files above the size cap are skipped
    with a reason instead of being copied.
    """
    try:
        info = os.lstat(source_path)
    except OSError as exc:
        raise BackupError("cannot inspect %r: %s" % (source_path, exc))
    if is_reparse_stat(info):
        return [], [{"path": source_path, "reason": "reparse_point"}]
    if not os.path.isdir(source_path):
        if os.path.basename(source_path) in CONFIG_FILENAMES:
            if info.st_size > max_file_bytes:
                return [], [{"path": source_path, "reason": "too_large"}]
            return [source_path], []
        return [], []
    return _discover_in_directory(source_path, max_depth, max_file_bytes)


def _selected_files(source_path, config_files, max_file_bytes):
    # type: (str, tuple, int) -> tuple
    if config_files:
        selected = []
        skipped = []
        for item in config_files:
            candidate = item
            if not os.path.isabs(candidate):
                candidate = os.path.join(source_path, item)
            candidate = os.path.normpath(candidate)
            try:
                info = os.lstat(candidate)
            except OSError as exc:
                raise BackupError(
                    "cannot back up %r: %s" % (candidate, exc)
                )
            if is_reparse_stat(info) or not os.path.isfile(candidate):
                raise BackupError(
                    "%r is not a regular file" % candidate
                )
            if info.st_size > max_file_bytes:
                skipped.append(
                    {
                        "path": _relative_to_root(source_path, candidate),
                        "reason": "too_large",
                    }
                )
                continue
            selected.append(candidate)
        return selected, skipped
    return discover_config_files(source_path, max_file_bytes=max_file_bytes)


def _copy_and_hash(source, destination):
    # type: (str, str) -> tuple
    expected = os.stat(source).st_size
    hasher = hashlib.sha256()
    copied = 0
    with open(source, "rb") as reader, open(destination, "wb") as writer:
        while True:
            chunk = reader.read(CHUNK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)
            writer.write(chunk)
            copied += len(chunk)
    if copied != expected:
        raise BackupError(
            "config file changed while backing up: %s" % source
        )
    if os.stat(destination).st_size != copied:
        raise BackupError("short write while backing up: %s" % source)
    return copied, hasher.hexdigest()


def _write_manifest(backup_dir, manifest):
    # type: (str, object) -> str
    target = os.path.join(backup_dir, MANIFEST_NAME)
    temporary = target + ".tmp"
    payload = json.dumps(
        manifest.to_dict(), ensure_ascii=False, indent=2, sort_keys=True
    )
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.write("\n")
    os.replace(temporary, target)
    return target


def backup_config(
    tool_id,
    source_path,
    backup_root,
    job_id,
    config_files=(),
    max_file_bytes=MAX_CONFIG_FILE_BYTES,
    now=None,
):
    # type: (str, str, str, str, tuple, int, object) -> object
    """Back up selected config files and return a ``BackupManifest``.

    Any read/write failure raises ``BackupError``; the caller owns the
    half-written backup directory and removes it using the job naming rule.
    """
    destination = paths.backup_dir(backup_root, tool_id, job_id, now=now)
    files_root = os.path.join(destination, FILES_DIR_NAME)
    os.makedirs(files_root)

    selected, skipped = _selected_files(source_path, config_files, max_file_bytes)
    entries = []
    used_names = set()
    for source in selected:
        rel = _relative_to_root(source_path, source)
        key = os.path.normcase(rel)
        if key in used_names:
            raise BackupError("duplicate config file name: %s" % rel)
        used_names.add(key)

        target = os.path.join(files_root, *rel.split("/"))
        os.makedirs(os.path.dirname(target), exist_ok=True)

        try:
            size, digest = _copy_and_hash(source, target)
        except BackupError:
            raise
        except OSError as exc:
            raise BackupError("cannot back up %s: %s" % (source, exc))

        entries.append(
            BackupEntry(
                rel_path=rel,
                source_path=source,
                backup_path=target,
                size=size,
                sha256=digest,
            )
        )

    manifest = BackupManifest(
        schema_version=1,
        job_id=job_id,
        tool_id=tool_id,
        source_root=os.path.abspath(source_path),
        backup_dir=destination,
        created_at=(now or datetime.datetime.now())
        .astimezone()
        .isoformat(),
        entries=entries,
        skipped=skipped,
    )
    try:
        _write_manifest(destination, manifest)
    except OSError as exc:
        raise BackupError("cannot write backup manifest: %s" % exc)
    return manifest


__all__ = [
    "BackupError",
    "CONFIG_FILENAMES",
    "FILES_DIR_NAME",
    "MANIFEST_NAME",
    "MAX_CONFIG_DEPTH",
    "MAX_CONFIG_FILE_BYTES",
    "backup_config",
    "discover_config_files",
]
