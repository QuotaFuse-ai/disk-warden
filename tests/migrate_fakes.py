# -*- coding: utf-8 -*-
"""Shared fixtures for the T10 migration tests.

``FakeOps`` simulates junctions in memory while every real path stays a
normal directory, so the unit tests never call ``mklink``.  The real junction
path is exercised by ``tools/migrate_drill_b.py`` on a temporary sandbox.
"""

import os
import shutil

from src.core.migrate import (
    MigrationOps,
    MigrationResult,
    VerifyResult,
    write_owner_marker,
)

JOB_ID = "0123456789abcdef"
JOB8 = "01234567"
FILE_PAYLOADS = {
    "a.bin": b"a" * 1000,
    "sub/b.bin": b"b" * 2048,
}
TOTAL_BYTES = sum(len(payload) for payload in FILE_PAYLOADS.values())


class Process(object):
    """Minimal process-info shape accepted by ``detect_processes``."""

    def __init__(self, pid, name, exe=""):
        self.pid = pid
        self.name = name
        self.exe = exe


class FakeOps(MigrationOps):
    """In-memory junctions on top of the real filesystem."""

    def __init__(self):
        self.links = {}
        self.fail_create_link = None
        self.fail_rename = None
        self.break_link_reads = False

    @staticmethod
    def _key(path):
        return os.path.normcase(
            os.path.abspath(os.path.normpath(str(path)))
        )

    def _link_target(self, path):
        return self.links.get(self._key(path))

    def _resolve(self, path):
        absolute = os.path.abspath(str(path))
        key = os.path.normcase(absolute)
        for link_key in sorted(self.links, key=len, reverse=True):
            if key == link_key:
                return self.links[link_key]
            prefix = link_key + os.sep
            if key.startswith(prefix):
                return self.links[link_key] + absolute[len(link_key):]
        return absolute

    def exists(self, path):
        return self._key(path) in self.links or os.path.lexists(str(path))

    def is_dir(self, path):
        if self._key(path) in self.links:
            return True
        return os.path.isdir(str(path))

    def is_link(self, path):
        return self._key(path) in self.links

    def is_junction(self, path):
        return self._key(path) in self.links

    def read_link_target(self, path):
        return self._link_target(path)

    def create_link(self, link_path, target_path):
        if self.fail_create_link:
            raise OSError(self.fail_create_link)
        if not os.path.isdir(target_path):
            raise OSError("link target is not a directory")
        self.links[self._key(link_path)] = os.path.abspath(target_path)
        return link_path

    def remove_link(self, path, expected_target=None):
        key = self._key(path)
        if key not in self.links:
            return False
        self.links.pop(key)
        return True

    def rename(self, source, destination):
        if self.fail_rename is not None and self.fail_rename(source, destination):
            raise OSError("injected rename failure")
        key = self._key(source)
        if key in self.links:
            target = self.links.pop(key)
            self.links[self._key(destination)] = target
            return destination
        os.rename(str(source), str(destination))
        return destination

    def same_volume(self, left, right):
        return True

    def read_bytes(self, path, size=1):
        resolved = self._resolve(path)
        if self.break_link_reads and self._key(resolved) != self._key(path):
            return b"\x00" * size
        with open(resolved, "rb") as handle:
            return handle.read(size)

    def list_dir(self, path):
        with os.scandir(self._resolve(path)) as entries:
            return sorted(entry.name for entry in entries)


class MigrationFixture(object):
    """A source tree, a target root and the job's derived paths."""

    def __init__(self, root):
        self.root = root
        self.source_parent = root / "Cursor"
        self.source = self.source_parent / "Cache"
        self.source.mkdir(parents=True)
        self.tool_id = "demo"
        self.tool_dir = root / "data" / "demo"
        self.tool_dir.mkdir(parents=True)
        self.job_id = JOB_ID
        self.final = self.tool_dir / "Cache"
        self.staging_parent = self.tool_dir / (".dw-partial-" + JOB8)
        self.staging = self.staging_parent / "Cache"
        self.tombstone = self.source_parent / (".dwtombstone-" + JOB8)
        for rel, payload in FILE_PAYLOADS.items():
            write_file(self.source, rel, payload)

    def make_staging(self):
        shutil.copytree(str(self.source), str(self.staging))
        return self.staging

    def make_final(self, owned=True, base=None):
        origin = base or (self.source if self.source.exists() else self.tombstone)
        shutil.copytree(str(origin), str(self.final))
        if owned:
            write_owner_marker(
                str(self.final),
                self.job_id,
                self.tool_id,
                str(self.source),
                str(self.final),
                file_count=len(FILE_PAYLOADS),
                total_bytes=TOTAL_BYTES,
            )
        return self.final

    def move_source_to_tombstone(self):
        os.rename(str(self.source), str(self.tombstone))
        return self.tombstone

    def make_result(self, ok=True):
        counts = len(FILE_PAYLOADS) if ok else 0
        total = TOTAL_BYTES if ok else 0
        verify = VerifyResult(
            ok=ok,
            source_file_count=counts,
            target_file_count=counts,
            source_bytes=total,
            target_bytes=total,
        )
        return MigrationResult(
            job_id=self.job_id,
            tool_id=self.tool_id,
            source_path=str(self.source),
            final_path=str(self.final),
            staging_path=str(self.staging),
            ok=ok,
            verify=verify,
        )


def write_file(base, rel, payload):
    path = base.joinpath(*rel.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def snapshot(base):
    base = str(base)
    return {
        os.path.relpath(root, base).replace(os.sep, "/"): read_all(root)
        for root in _files(base)
    }


def _files(base):
    for directory, _dirs, names in os.walk(base):
        for name in sorted(names):
            yield os.path.join(directory, name)


def read_all(path):
    with open(path, "rb") as handle:
        return handle.read()


def expected_snapshot():
    return dict(FILE_PAYLOADS)
