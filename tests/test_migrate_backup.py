# -*- coding: utf-8 -*-
"""Unit tests for the T09 config backup layer (S1)."""

import datetime
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.migrate.backup import (  # noqa: E402
    MANIFEST_NAME,
    BackupError,
    backup_config,
    discover_config_files,
)

JOB_ID = "abcdef0123456789abcdef0123456789"


class BackupTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory(prefix="dw-t09-backup-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.tool_root = self.root / "Cursor"
        self.tool_root.mkdir()
        self.backup_root = self.root / "backup"

    def test_discovers_only_known_config_names_within_depth(self):
        (self.tool_root / "settings.json").write_text("{}", encoding="utf-8")
        (self.tool_root / "User").mkdir()
        (self.tool_root / "User" / "keybindings.json").write_text(
            "[]", encoding="utf-8"
        )
        cache = self.tool_root / "Cache"
        cache.mkdir()
        (cache / "data.bin").write_bytes(b"x" * 4096)
        (cache / "index.json").write_text("{}", encoding="utf-8")
        (self.tool_root / "a" / "b").mkdir(parents=True)
        (self.tool_root / "a" / "b" / "settings.json").write_text(
            "{}", encoding="utf-8"
        )
        (self.tool_root / "a" / "b" / "c").mkdir()
        (self.tool_root / "a" / "b" / "c" / "settings.json").write_text(
            "{}", encoding="utf-8"
        )

        selected, skipped = discover_config_files(str(self.tool_root))
        rel_paths = sorted(
            os.path.relpath(path, str(self.tool_root)).replace("\\", "/")
            for path in selected
        )

        self.assertEqual(
            rel_paths,
            ["User/keybindings.json", "a/b/settings.json", "settings.json"],
        )
        self.assertEqual(skipped, [])

    def test_oversized_config_is_skipped_not_copied(self):
        (self.tool_root / "settings.json").write_bytes(b"x" * 64)
        selected, skipped = discover_config_files(
            str(self.tool_root), max_file_bytes=16
        )
        self.assertEqual(selected, [])
        self.assertEqual(
            skipped, [{"path": "settings.json", "reason": "too_large"}]
        )

    def test_backup_copies_config_and_writes_sha256_manifest(self):
        payload = '{"theme": "dark"}'
        (self.tool_root / "settings.json").write_text(payload, encoding="utf-8")
        (self.tool_root / "User").mkdir()
        (self.tool_root / "User" / "keybindings.json").write_text(
            "[]", encoding="utf-8"
        )
        (self.tool_root / "Cache").mkdir()
        (self.tool_root / "Cache" / "blob.bin").write_bytes(b"data")

        moment = datetime.datetime(2026, 10, 19, 12, 0, 0)
        manifest = backup_config(
            "demo", str(self.tool_root), str(self.backup_root), JOB_ID, now=moment
        )

        self.assertEqual(
            os.path.basename(manifest.backup_dir),
            "2026-10-19_120000_demo_abcdef01",
        )
        self.assertEqual(manifest.file_count, 2)
        self.assertEqual(
            sorted(entry.rel_path for entry in manifest.entries),
            ["User/keybindings.json", "settings.json"],
        )
        self.assertNotIn(
            "blob.bin", [entry.rel_path for entry in manifest.entries]
        )

        manifest_path = os.path.join(manifest.backup_dir, MANIFEST_NAME)
        with open(manifest_path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        self.assertEqual(raw["schema_version"], 1)
        self.assertEqual(raw["job_id"], JOB_ID)
        self.assertEqual(raw["file_count"], 2)
        self.assertEqual(raw["backup_dir"], manifest.backup_dir)

        settings_entry = [
            entry
            for entry in raw["entries"]
            if entry["rel_path"] == "settings.json"
        ][0]
        expected_digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        self.assertEqual(settings_entry["sha256"], expected_digest)
        with open(settings_entry["backup_path"], "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), payload)

    def test_explicit_config_file_is_backed_up(self):
        (self.tool_root / "custom.cfg").write_text("cfg", encoding="utf-8")
        (self.tool_root / "data.bin").write_bytes(b"not config")

        manifest = backup_config(
            "demo",
            str(self.tool_root),
            str(self.backup_root),
            JOB_ID,
            config_files=("custom.cfg",),
        )

        self.assertEqual(
            [entry.rel_path for entry in manifest.entries], ["custom.cfg"]
        )

    def test_missing_explicit_config_raises(self):
        with self.assertRaises(BackupError):
            backup_config(
                "demo",
                str(self.tool_root),
                str(self.backup_root),
                JOB_ID,
                config_files=("missing.json",),
            )

    def test_empty_config_selection_is_still_a_complete_backup(self):
        (self.tool_root / "Cache").mkdir()
        (self.tool_root / "Cache" / "data.bin").write_bytes(b"x")

        manifest = backup_config(
            "demo", str(self.tool_root), str(self.backup_root), JOB_ID
        )

        self.assertEqual(manifest.file_count, 0)
        self.assertEqual(manifest.total_bytes, 0)
        self.assertTrue(
            os.path.isfile(os.path.join(manifest.backup_dir, MANIFEST_NAME))
        )


if __name__ == "__main__":
    unittest.main()
