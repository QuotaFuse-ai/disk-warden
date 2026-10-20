# -*- coding: utf-8 -*-
"""Unit tests for V2 verification (path set, size and content, counts)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.migrate import paths as migrate_paths  # noqa: E402
from src.core.migrate.verify import (  # noqa: E402
    FULL_DIGEST_PREFIX,
    FULL_HASH_LIMIT,
    SAMPLED_DIGEST_PREFIX,
    content_fingerprint,
    verify_trees,
)


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory(prefix="dw-t09-verify-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.source = self.root / "source"
        self.target = self.root / "target"
        self.source.mkdir()
        self.target.mkdir()

    def write(self, base, rel, payload):
        path = base.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(payload, bytes):
            path.write_bytes(payload)
        else:
            path.write_text(payload, encoding="utf-8")
        return path

    def test_identical_trees_pass(self):
        self.write(self.source, "a.bin", b"a" * 10)
        self.write(self.source, "sub/b.bin", b"b" * 20)
        self.write(self.target, "a.bin", b"a" * 10)
        self.write(self.target, "sub/b.bin", b"b" * 20)
        # Empty directories are not compared: robocopy /E copies them, but
        # they carry no data.
        (self.source / "empty").mkdir()

        result = verify_trees(str(self.source), str(self.target))

        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.source_file_count, 2)
        self.assertEqual(result.target_file_count, 2)
        self.assertEqual(result.source_bytes, 30)
        self.assertEqual(result.target_bytes, 30)

    def test_size_mismatch_fails_even_when_counts_match(self):
        self.write(self.source, "a.bin", b"a" * 10)
        self.write(self.target, "a.bin", b"a" * 9)

        result = verify_trees(str(self.source), str(self.target))

        self.assertFalse(result.ok)
        self.assertEqual(result.source_file_count, 1)
        self.assertEqual(result.target_file_count, 1)
        self.assertEqual(result.size_mismatch_count, 1)
        self.assertEqual(result.size_mismatches[0].rel_path, "a.bin")
        self.assertEqual(result.size_mismatches[0].source_bytes, 10)
        self.assertEqual(result.size_mismatches[0].target_bytes, 9)
        self.assertEqual(result.source_bytes, 10)
        self.assertEqual(result.target_bytes, 9)
        self.assertIn("total bytes differ", result.reason)

    def test_missing_and_extra_files_are_reported(self):
        self.write(self.source, "kept.bin", b"x")
        self.write(self.source, "missing.bin", b"y")
        self.write(self.target, "kept.bin", b"x")
        self.write(self.target, "extra.bin", b"z")

        result = verify_trees(str(self.source), str(self.target))

        self.assertFalse(result.ok)
        self.assertEqual(result.missing_count, 1)
        self.assertEqual(result.extra_count, 1)
        self.assertEqual(result.missing, ["missing.bin"])
        self.assertEqual(result.extra, ["extra.bin"])
        self.assertIn("path set differs", result.reason)

    def test_access_error_fails_verification(self):
        self.write(self.source, "a.bin", b"a")
        self.write(self.target, "a.bin", b"a")

        real_scandir = os.scandir
        target_key = os.path.normcase(os.path.abspath(str(self.target)))

        def fake_scandir(path):
            if os.path.normcase(os.path.abspath(str(path))) == target_key:
                raise PermissionError(13, "access denied", str(path))
            return real_scandir(path)

        with mock.patch("src.core.migrate.verify.os.scandir", new=fake_scandir):
            result = verify_trees(str(self.source), str(self.target))

        self.assertFalse(result.ok)
        self.assertTrue(result.errors)
        self.assertIn("access/read error", result.reason)

    def test_file_root_is_compared(self):
        source_file = self.root / "settings.json"
        target_file = self.root / "copy.json"
        source_file.write_bytes(b"abc")
        target_file.write_bytes(b"abc")

        result = verify_trees(str(source_file), str(target_file))
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.source_file_count, 1)

        target_file.write_bytes(b"abcd")
        result = verify_trees(str(source_file), str(target_file))
        self.assertFalse(result.ok)
        self.assertEqual(result.size_mismatch_count, 1)

    def test_reparse_points_are_recorded_but_not_followed(self):
        self.write(self.source, "a.bin", b"a")
        self.write(self.target, "a.bin", b"a")
        (self.source / "link").mkdir()
        (self.target / "link").mkdir()
        self.write(self.source, "link/inside.bin", b"would be double counted")
        self.write(self.target, "link/inside.bin", b"would be double counted")

        real_scandir = os.scandir

        class FakeStat(object):
            st_mode = 0o040700  # directory
            st_size = 0
            st_file_attributes = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT

        class FakeEntry(object):
            def __init__(self, entry):
                self.name = entry.name
                self.path = entry.path

            def stat(self, follow_symlinks=True):
                return FakeStat()

        class FakeScandir(object):
            def __init__(self, entries):
                self._entries = entries

            def __enter__(self):
                return iter(self._entries)

            def __exit__(self, *exc_info):
                return False

        def fake_scandir(path):
            entries = [
                FakeEntry(entry) if entry.name == "link" else entry
                for entry in real_scandir(path)
            ]
            return FakeScandir(entries)

        # Python 3.8 reports st_ino == 0 for DirEntry.stat(follow_symlinks=
        # False) on Windows, so mark the junctions by wrapping scandir
        # instead of matching inodes.
        with mock.patch("src.core.migrate.verify.os.scandir", new=fake_scandir):
            result = verify_trees(str(self.source), str(self.target))

        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.reparse_paths, ["link"])
        self.assertEqual(result.source_file_count, 1)


class ContentVerificationTests(unittest.TestCase):
    """T26/R-02: identical size is not evidence of identical bytes."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory(prefix="dw-t26-verify-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.source = self.root / "source"
        self.target = self.root / "target"
        self.source.mkdir()
        self.target.mkdir()

    def write(self, base, rel, payload):
        path = base.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def test_same_size_different_content_fails(self):
        self.write(self.source, "a.bin", b"a" * 64)
        self.write(self.target, "a.bin", b"b" * 64)

        result = verify_trees(str(self.source), str(self.target))

        self.assertFalse(result.ok)
        self.assertEqual(result.size_mismatch_count, 0)
        self.assertEqual(result.content_mismatch_count, 1)
        self.assertEqual(result.content_mismatches[0].rel_path, "a.bin")
        self.assertIn("differ in content", result.reason)
        self.assertEqual(
            result.to_dict()["content_mismatches"], [{"rel_path": "a.bin"}]
        )

    def test_same_bytes_with_different_mtime_pass(self):
        payload = b"unchanged" * 100
        self.write(self.source, "a.bin", payload)
        target_file = self.write(self.target, "a.bin", payload)
        os.utime(str(target_file), (1, 1))

        result = verify_trees(str(self.source), str(self.target))

        self.assertTrue(result.ok, result.reason)

    def test_large_file_only_tail_changed_fails(self):
        size = FULL_HASH_LIMIT + 1024 * 1024
        payload = bytearray(size)
        payload[-1] = 0x41
        self.write(self.source, "big.bin", bytes(payload))
        target_file = self.write(self.target, "big.bin", bytes(payload))

        result = verify_trees(str(self.source), str(self.target))
        self.assertTrue(result.ok, result.reason)

        damaged = bytearray(payload)
        damaged[-1] = 0x42
        target_file.write_bytes(bytes(damaged))

        result = verify_trees(str(self.source), str(self.target))

        self.assertFalse(result.ok)
        self.assertEqual(result.content_mismatch_count, 1)
        self.assertEqual(result.content_mismatches[0].rel_path, "big.bin")

    def test_fingerprint_uses_full_hash_up_to_the_limit(self):
        small = self.write(self.source, "small.bin", b"x" * 16)
        large = self.write(
            self.source, "large.bin", b"y" * (FULL_HASH_LIMIT + 1)
        )

        self.assertTrue(
            content_fingerprint(str(small), small.stat().st_size).startswith(
                FULL_DIGEST_PREFIX
            )
        )
        self.assertTrue(
            content_fingerprint(str(large), large.stat().st_size).startswith(
                SAMPLED_DIGEST_PREFIX
            )
        )

    def test_file_root_with_same_size_different_bytes_fails(self):
        source_file = self.root / "settings.json"
        target_file = self.root / "copy.json"
        source_file.write_bytes(b"abc")
        target_file.write_bytes(b"abd")

        result = verify_trees(str(source_file), str(target_file))

        self.assertFalse(result.ok)
        self.assertEqual(result.content_mismatch_count, 1)
        self.assertEqual(result.content_mismatches[0].rel_path, ".")

    def test_ignore_paths_skips_the_ownership_marker(self):
        job_id = "0123456789abcdef"
        marker_name = migrate_paths.owner_marker_name(job_id)
        self.write(self.source, "a.bin", b"a")
        self.write(self.target, "a.bin", b"a")
        self.write(self.target, marker_name, b"{}")

        without_ignore = verify_trees(str(self.source), str(self.target))
        self.assertFalse(without_ignore.ok)
        self.assertEqual(without_ignore.extra_count, 1)

        with_ignore = verify_trees(
            str(self.source), str(self.target), ignore_paths=(marker_name,)
        )
        self.assertTrue(with_ignore.ok, with_ignore.reason)
        self.assertEqual(with_ignore.target_file_count, 1)


if __name__ == "__main__":
    unittest.main()
