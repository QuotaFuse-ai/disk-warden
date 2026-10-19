# -*- coding: utf-8 -*-
"""Atomic-write and corrupt-read coverage for the store layer (AC-STORE-02)."""

import codecs
import datetime
import os
import tempfile
import unittest
from unittest import mock

from src.core.store import atomic


class AtomicWriteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.path = os.path.join(self.root, "settings.json")

    def _temps(self):
        return [
            name
            for name in os.listdir(self.root)
            if atomic.is_temp_name(name)
        ]

    def test_atomic_write_creates_nested_directory(self):
        target = os.path.join(self.root, "a", "b", "data.json")
        atomic.atomic_write_bytes(target, b"{}\n")
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), b"{}\n")

    def test_interrupted_replace_keeps_original_and_cleans_temp(self):
        atomic.atomic_write_bytes(self.path, b'{"value": 1}\n')

        with mock.patch.object(
            atomic.os, "replace", side_effect=OSError("replace failed")
        ):
            with self.assertRaises(atomic.StoreError):
                atomic.atomic_write_bytes(self.path, b'{"value": 2}\n')

        with open(self.path, "rb") as handle:
            self.assertEqual(handle.read(), b'{"value": 1}\n')
        self.assertEqual(self._temps(), [])

    def test_interrupted_fsync_keeps_original_and_cleans_temp(self):
        atomic.atomic_write_bytes(self.path, b'{"value": 1}\n')

        with mock.patch.object(
            atomic.os, "fsync", side_effect=OSError("fsync failed")
        ):
            with self.assertRaises(atomic.StoreError):
                atomic.atomic_write_bytes(self.path, b'{"value": 2}\n')

        with open(self.path, "rb") as handle:
            self.assertEqual(handle.read(), b'{"value": 1}\n')
        self.assertEqual(self._temps(), [])

    def test_interrupted_write_does_not_create_target(self):
        with mock.patch.object(
            atomic.os, "replace", side_effect=OSError("replace failed")
        ):
            with self.assertRaises(atomic.StoreError):
                atomic.atomic_write_json(self.path, {"value": 2})
        self.assertFalse(os.path.exists(self.path))

    def test_bom_is_stripped_on_read(self):
        with open(self.path, "wb") as handle:
            handle.write(codecs.BOM_UTF8 + b'{"value": 1}')
        self.assertEqual(atomic.read_json_file(self.path), {"value": 1})

    def test_empty_file_is_corrupt(self):
        with open(self.path, "wb") as handle:
            handle.write(b"  \n")
        with self.assertRaises(atomic.StoreCorruptError):
            atomic.read_json_file(self.path)

    def test_invalid_utf8_is_corrupt(self):
        with open(self.path, "wb") as handle:
            handle.write(b"\xff\xfe\x00")
        with self.assertRaises(atomic.StoreCorruptError):
            atomic.read_json_file(self.path)

    def test_invalid_json_is_corrupt(self):
        with open(self.path, "wb") as handle:
            handle.write(b'{"value": ')
        with self.assertRaises(atomic.StoreCorruptError):
            atomic.read_json_file(self.path)

    def test_dump_json_sorts_keys_and_appends_newline(self):
        payload = atomic.dump_json_bytes({"b": 1, "a": "\u4e2d"})
        self.assertEqual(payload, b'{\n  "a": "\xe4\xb8\xad",\n  "b": 1\n}\n')

    def test_dump_json_rejects_unserialisable_payload(self):
        with self.assertRaises(atomic.StoreError):
            atomic.dump_json_bytes({"bad": object()})

    def test_backup_refuses_to_copy_a_corrupt_current_file(self):
        with open(self.path, "wb") as handle:
            handle.write(b"not json")
        self.assertEqual(atomic.backup_path(self.path), "")
        self.assertFalse(os.path.exists(self.path + ".bak"))

    def test_backup_copies_a_valid_current_file(self):
        atomic.atomic_write_json(self.path, {"value": 1})
        self.assertTrue(atomic.backup_path(self.path))
        with open(self.path + ".bak", "rb") as handle:
            self.assertEqual(handle.read(), atomic.dump_json_bytes({"value": 1}))

    def test_quarantine_renames_and_avoids_collisions(self):
        with open(self.path, "wb") as handle:
            handle.write(b"bad")
        first = atomic.quarantine_corrupt(self.path)
        self.assertIn(".corrupt-", first)
        self.assertFalse(os.path.exists(self.path))

        with open(self.path, "wb") as handle:
            handle.write(b"bad again")
        second = atomic.quarantine_corrupt(self.path)
        self.assertNotEqual(first, second)
        self.assertTrue(os.path.exists(first))
        self.assertTrue(os.path.exists(second))

    def test_load_json_returns_default_and_quarantines(self):
        with open(self.path, "wb") as handle:
            handle.write(b"bad")
        self.assertEqual(atomic.load_json(self.path, default={"x": 1}), {"x": 1})
        self.assertFalse(os.path.exists(self.path))
        self.assertTrue(
            any(".corrupt-" in name for name in os.listdir(self.root))
        )

    def test_load_json_missing_returns_default(self):
        self.assertEqual(atomic.load_json(self.path, default=[]), [])

    def test_utc_now_formats_with_seconds_and_z(self):
        moment = datetime.datetime(2026, 1, 2, 3, 4, 5, 999999)
        self.assertEqual(atomic.utc_now(moment), "2026-01-02T03:04:05Z")
        aware = datetime.datetime(
            2026, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc
        )
        self.assertEqual(atomic.utc_now(aware), "2026-01-02T03:04:05Z")


if __name__ == "__main__":
    unittest.main()
