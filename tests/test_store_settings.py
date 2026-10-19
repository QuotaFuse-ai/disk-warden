# -*- coding: utf-8 -*-
"""Settings persistence: defaults, repair, validation and ``.bak`` fallback."""

import os
import tempfile
import unittest

from src.core.store import atomic
from src.core.store.settings import (
    DEFAULT_SETTINGS,
    SCHEMA_VERSION,
    SettingsStore,
    default_settings,
    validate_settings,
)


def codes(store):
    return [warning["code"] for warning in store.warnings]


class SettingsStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.path = os.path.join(self.root, "config", "settings.json")
        self.store = SettingsStore(self.path)

    def _write(self, payload):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "wb") as handle:
            handle.write(atomic.dump_json_bytes(payload))

    def test_missing_file_returns_defaults_and_marks_repair(self):
        settings = self.store.load()
        self.assertEqual(settings, DEFAULT_SETTINGS)
        self.assertTrue(self.store.needs_repair)
        self.assertIn("settings_defaults", codes(self.store))

    def test_repair_writes_defaults(self):
        settings = self.store.load(repair=True)
        self.assertTrue(os.path.isfile(self.path))
        self.assertFalse(self.store.needs_repair)
        self.assertEqual(atomic.read_json_file(self.path), settings)

    def test_save_and_load_round_trip(self):
        settings = default_settings()
        settings["launch_at_startup"] = True
        settings["language"] = "zh-CN"
        self.store.save(settings)
        self.assertEqual(self.store.load(), settings)

    def test_unknown_keys_are_preserved(self):
        self._write({"schema_version": 1, "future_key": {"nested": True}})
        settings = self.store.load()
        self.assertEqual(settings["future_key"], {"nested": True})
        self.assertFalse(self.store.needs_repair)

    def test_invalid_known_keys_fall_back_with_warnings(self):
        payload = {
            "schema_version": 1,
            "launch_at_startup": "yes",
            "language": 3,
            "uninstall_policy": "destroy",
        }
        self._write(payload)
        settings = self.store.load()
        self.assertFalse(settings["launch_at_startup"])
        self.assertEqual(settings["language"], "en")
        self.assertEqual(settings["uninstall_policy"], "keep")
        self.assertEqual(codes(self.store).count("settings_invalid"), 3)

    def test_strict_validation_raises(self):
        with self.assertRaises(atomic.StoreError):
            validate_settings({"auto_maintenance": 1}, strict=True)

    def test_save_rejects_invalid_settings(self):
        with self.assertRaises(atomic.StoreError):
            self.store.save({"uninstall_policy": "nuke"})

    def test_non_object_payload_returns_defaults(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "wb") as handle:
            handle.write(b"[]")
        settings = self.store.load()
        self.assertEqual(settings["schema_version"], SCHEMA_VERSION)
        self.assertIn("settings_invalid", codes(self.store))

    def test_older_schema_version_marks_repair(self):
        self._write({"schema_version": 0})
        self.store.load()
        self.assertTrue(self.store.needs_repair)

    def test_corrupt_file_is_quarantined_and_backup_loaded(self):
        good = default_settings()
        good["language"] = "zh-CN"
        self.store.save(good)
        newer = default_settings()
        newer["language"] = "en"
        self.store.save(newer)
        with open(self.path, "wb") as handle:
            handle.write(b"{broken")

        settings = self.store.load()
        self.assertEqual(settings["language"], "zh-CN")
        self.assertTrue(self.store.needs_repair)
        self.assertIn("settings_corrupt", codes(self.store))
        self.assertIn("settings_restored_from_backup", codes(self.store))
        self.assertFalse(os.path.exists(self.path))
        self.assertTrue(
            any(".corrupt-" in name for name in os.listdir(os.path.dirname(self.path)))
        )

    def test_corrupt_file_without_backup_uses_defaults(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "wb") as handle:
            handle.write(b"{broken")
        settings = self.store.load()
        self.assertEqual(settings, DEFAULT_SETTINGS)
        self.assertIn("settings_defaults", codes(self.store))

    def test_save_keeps_previous_valid_file_as_backup(self):
        first = default_settings()
        first["language"] = "zh-CN"
        self.store.save(first)
        second = default_settings()
        second["language"] = "fr"
        self.store.save(second)

        self.assertEqual(atomic.read_json_file(self.path)["language"], "fr")
        self.assertEqual(
            atomic.read_json_file(self.path + ".bak")["language"], "zh-CN"
        )

    def test_update_merges_and_persists(self):
        self.store.save(default_settings())
        updated = self.store.update({"auto_maintenance": True})
        self.assertTrue(updated["auto_maintenance"])
        self.assertTrue(self.store.load()["auto_maintenance"])

    def test_update_requires_object(self):
        with self.assertRaises(atomic.StoreError):
            self.store.update([])

    def test_empty_path_is_rejected(self):
        with self.assertRaises(atomic.StoreError):
            SettingsStore("")


if __name__ == "__main__":
    unittest.main()
