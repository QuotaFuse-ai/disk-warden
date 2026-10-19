# -*- coding: utf-8 -*-
"""Bootstrap pointer: explicit temp paths only, never the real profile dirs."""

import datetime
import os
import tempfile
import unittest
from unittest import mock

from src.core.store import atomic
from src.core.store.layout import DataLayout
from src.core.store.bootstrap import (
    ENV_BOOTSTRAP_PATH,
    SCHEMA_VERSION,
    default_bootstrap_path,
    load_bootstrap,
    save_bootstrap,
)

MOMENT = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)


class BootstrapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "bootstrap.json")
        self.root = os.path.join(self.tmp.name, "DiskWarden")

    def test_save_and_load_round_trip(self):
        record = save_bootstrap(self.root, path=self.path, now=MOMENT)
        self.assertEqual(record.data_root, os.path.abspath(self.root))
        self.assertEqual(record.updated_at, "2026-01-02T03:04:05Z")

        loaded = load_bootstrap(self.path)
        self.assertEqual(loaded.schema_version, SCHEMA_VERSION)
        self.assertEqual(loaded.data_root, os.path.abspath(self.root))

    def test_missing_file_returns_none(self):
        self.assertIsNone(load_bootstrap(self.path))

    def test_corrupt_file_is_quarantined(self):
        with open(self.path, "wb") as handle:
            handle.write(b"{broken")
        self.assertIsNone(load_bootstrap(self.path))
        self.assertTrue(
            any(".corrupt-" in name for name in os.listdir(self.tmp.name))
        )

    def test_non_object_payload_is_quarantined(self):
        with open(self.path, "wb") as handle:
            handle.write(atomic.dump_json_bytes(["nope"]))
        self.assertIsNone(load_bootstrap(self.path))
        self.assertFalse(os.path.exists(self.path))

    def test_payload_without_data_root_returns_none(self):
        with open(self.path, "wb") as handle:
            handle.write(atomic.dump_json_bytes({"schema_version": 1}))
        self.assertIsNone(load_bootstrap(self.path))

    def test_blank_data_root_returns_none(self):
        with open(self.path, "wb") as handle:
            handle.write(atomic.dump_json_bytes({"data_root": "   "}))
        self.assertIsNone(load_bootstrap(self.path))

    def test_invalid_schema_version_falls_back(self):
        with open(self.path, "wb") as handle:
            handle.write(
                atomic.dump_json_bytes({"data_root": self.root, "schema_version": "x"})
            )
        self.assertEqual(load_bootstrap(self.path).schema_version, SCHEMA_VERSION)

    def test_save_empty_root_falls_back_to_default(self):
        record = save_bootstrap("", path=self.path)
        self.assertEqual(record.data_root, DataLayout("").root)
        self.assertEqual(load_bootstrap(self.path).data_root, record.data_root)

    def test_default_path_honours_environment_override(self):
        override = os.path.join(self.tmp.name, "custom", "boot.json")
        with mock.patch.dict(os.environ, {ENV_BOOTSTRAP_PATH: override}):
            self.assertEqual(
                default_bootstrap_path(), os.path.abspath(override)
            )


if __name__ == "__main__":
    unittest.main()
