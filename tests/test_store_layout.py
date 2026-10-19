# -*- coding: utf-8 -*-
"""Data-root layout matches requirements 4.2/4.5."""

import os
import tempfile
import unittest
from unittest import mock

from src.core.store import atomic
from src.core.store.layout import (
    DataLayout,
    ENV_DATA_ROOT,
    ROOT_PROBE_PREFIX,
    can_change_root,
    default_data_root,
    root_has_migrated_data,
)


class LayoutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "DiskWarden")
        self.layout = DataLayout(self.root)

    def test_root_paths_match_documented_structure(self):
        self.assertEqual(self.layout.data_dir, os.path.join(self.root, "data"))
        self.assertEqual(self.layout.backup_dir, os.path.join(self.root, "backup"))
        self.assertEqual(self.layout.logs_dir, os.path.join(self.root, "logs"))
        self.assertEqual(
            self.layout.config_dir, os.path.join(self.root, "config")
        )
        self.assertEqual(
            self.layout.settings_path,
            os.path.join(self.root, "config", "settings.json"),
        )
        self.assertEqual(
            self.layout.learned_path,
            os.path.join(self.root, "config", "rules-learned.json"),
        )
        self.assertEqual(
            self.layout.history_path,
            os.path.join(self.root, "config", "history.json"),
        )
        self.assertEqual(
            self.layout.jobs_dir,
            os.path.join(self.root, "config", "migration-jobs"),
        )
        self.assertEqual(
            self.layout.locales_dir,
            os.path.join(self.root, "config", "locales"),
        )

    def test_ensure_creates_every_directory(self):
        self.layout.ensure()
        for directory in self.layout.directories:
            self.assertTrue(os.path.isdir(directory), directory)

    def test_ensure_is_idempotent_and_keeps_existing_files(self):
        self.layout.ensure()
        with open(self.layout.settings_path, "wb") as handle:
            handle.write(b"{}")
        self.layout.ensure()
        with open(self.layout.settings_path, "rb") as handle:
            self.assertEqual(handle.read(), b"{}")

    def test_is_writable_removes_its_probe(self):
        self.assertTrue(self.layout.is_writable())
        leftovers = [
            name
            for name in os.listdir(self.root)
            if name.startswith(ROOT_PROBE_PREFIX)
        ]
        self.assertEqual(leftovers, [])

    def test_is_writable_returns_false_when_root_cannot_be_created(self):
        blocker = os.path.join(self.tmp.name, "blocker")
        with open(blocker, "wb") as handle:
            handle.write(b"x")
        layout = DataLayout(os.path.join(blocker, "nested"))
        self.assertFalse(layout.is_writable())

    def test_has_migrated_data_ignores_dot_entries(self):
        self.assertFalse(self.layout.has_migrated_data())
        self.layout.ensure()
        with open(os.path.join(self.layout.data_dir, ".keep"), "wb") as handle:
            handle.write(b"")
        self.assertFalse(self.layout.has_migrated_data())
        os.makedirs(os.path.join(self.layout.data_dir, "demo"))
        self.assertTrue(self.layout.has_migrated_data())

    def test_can_change_root_only_when_no_migrated_data(self):
        other = os.path.join(self.tmp.name, "Other")
        self.assertTrue(can_change_root(self.root, other))
        self.layout.ensure()
        os.makedirs(os.path.join(self.layout.data_dir, "demo"))
        self.assertFalse(can_change_root(self.root, other))
        self.assertTrue(can_change_root(self.root, self.root))
        self.assertTrue(can_change_root(self.root, self.root + os.sep))

    def test_root_has_migrated_data_wrapper(self):
        self.assertFalse(root_has_migrated_data(self.root))

    def test_default_root_honours_environment_override(self):
        override = os.path.join(self.tmp.name, "EnvRoot")
        with mock.patch.dict(os.environ, {ENV_DATA_ROOT: override}):
            self.assertEqual(default_data_root(), os.path.abspath(override))

    def test_empty_root_is_rejected(self):
        with self.assertRaises(atomic.StoreError):
            DataLayout("   ")

    def test_to_dict_exposes_all_paths(self):
        payload = self.layout.to_dict()
        self.assertEqual(payload["root"], self.layout.root)
        self.assertEqual(payload["jobs_dir"], self.layout.jobs_dir)


if __name__ == "__main__":
    unittest.main()
