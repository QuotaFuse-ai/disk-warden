# -*- coding: utf-8 -*-
"""Unit tests for configuration restore (AC-ROL-06 cases A/B/C/D).

Every case runs against a temporary sandbox; no real user configuration is
read or written.
"""

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

from src.core.rollback.config import restore_configs  # noqa: E402
from src.core.rollback.models import (  # noqa: E402
    CONFIG_ACTION_BACKUP_CORRUPT,
    CONFIG_ACTION_BACKUP_MISSING,
    CONFIG_ACTION_IDENTICAL,
    CONFIG_ACTION_KEPT_USER,
    CONFIG_ACTION_RESTORED_MISSING,
    CONFIG_ACTION_RESTORED_USER_CHOICE,
    CONFIG_ACTION_RESTORE_FAILED,
    CONFIG_KEEP,
    CONFIG_RESTORE,
    CONFIG_STATUS_FAILED,
    CONFIG_STATUS_KEPT,
    CONFIG_STATUS_NOT_REQUIRED,
    CONFIG_STATUS_PARTIAL,
    CONFIG_STATUS_RESTORED,
    CONFIG_STATUS_UNAVAILABLE,
)

JOB_ID = "0123456789abcdef"


class ConfigRestoreTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory(prefix="dw-t11-config-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.source_root = self.root / "Cursor"
        self.source_root.mkdir()
        self.backup_dir = self.root / "backup"
        (self.backup_dir / "files").mkdir(parents=True)

    # -- helpers ---------------------------------------------------------

    def _backup_file(self, rel, payload):
        path = self.backup_dir / "files" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def _manifest(self, entries=None, **overrides):
        payload = {
            "schema_version": 1,
            "tool_id": "demo",
            "job_id": JOB_ID,
            "source_root": str(self.source_root),
            "entries": entries if entries is not None else [],
        }
        payload.update(overrides)
        (self.backup_dir / "manifest.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        return payload

    def _entry(self, rel, payload=None, **overrides):
        payload = payload if payload is not None else b'{"theme": "dark"}'
        entry = {
            "rel_path": rel,
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        entry.update(overrides)
        return entry

    def _actions(self, outcome):
        return [detail["action"] for detail in outcome.details]

    # -- case A ----------------------------------------------------------

    def test_identical_file_is_kept_without_rewrite(self):
        payload = b'{"theme": "dark"}'
        self._backup_file("settings.json", payload)
        target = self.source_root / "settings.json"
        target.write_bytes(payload)
        self._manifest([self._entry("settings.json", payload)])

        outcome = restore_configs(
            str(self.backup_dir),
            choice=CONFIG_KEEP,
            expected_tool_id="demo",
            expected_job_id=JOB_ID,
        )

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.status, CONFIG_STATUS_KEPT)
        self.assertEqual(outcome.kept, ["settings.json"])
        self.assertEqual(outcome.restored, [])
        self.assertEqual(self._actions(outcome), [CONFIG_ACTION_IDENTICAL])
        self.assertEqual(target.read_bytes(), payload)

    # -- case B ----------------------------------------------------------

    def test_missing_current_file_is_restored(self):
        payload = b'{"theme": "dark"}'
        self._backup_file("settings.json", payload)
        self._manifest([self._entry("settings.json", payload)])

        outcome = restore_configs(str(self.backup_dir))

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.status, CONFIG_STATUS_RESTORED)
        self.assertEqual(outcome.restored, ["settings.json"])
        self.assertEqual(self._actions(outcome), [CONFIG_ACTION_RESTORED_MISSING])
        self.assertEqual(
            (self.source_root / "settings.json").read_bytes(), payload
        )

    def test_nested_file_is_restored(self):
        payload = b'{"key": "value"}'
        self._backup_file("User/keybindings.json", payload)
        self._manifest([self._entry("User/keybindings.json", payload)])

        outcome = restore_configs(str(self.backup_dir))

        self.assertEqual(outcome.status, CONFIG_STATUS_RESTORED)
        self.assertEqual(
            (self.source_root / "User" / "keybindings.json").read_bytes(),
            payload,
        )

    # -- case C ----------------------------------------------------------

    def test_user_modified_file_is_kept_by_default(self):
        backup_payload = b'{"theme": "dark"}'
        user_payload = b'{"theme": "light"}'
        self._backup_file("settings.json", backup_payload)
        target = self.source_root / "settings.json"
        target.write_bytes(user_payload)
        self._manifest([self._entry("settings.json", backup_payload)])

        outcome = restore_configs(str(self.backup_dir), choice=CONFIG_KEEP)

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.status, CONFIG_STATUS_KEPT)
        self.assertEqual(self._actions(outcome), [CONFIG_ACTION_KEPT_USER])
        self.assertEqual(target.read_bytes(), user_payload)

    def test_explicit_restore_replaces_user_modified_file(self):
        backup_payload = b'{"theme": "dark"}'
        user_payload = b'{"theme": "light"}'
        self._backup_file("settings.json", backup_payload)
        target = self.source_root / "settings.json"
        target.write_bytes(user_payload)
        self._manifest([self._entry("settings.json", backup_payload)])

        outcome = restore_configs(str(self.backup_dir), choice=CONFIG_RESTORE)

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.status, CONFIG_STATUS_RESTORED)
        self.assertEqual(
            self._actions(outcome), [CONFIG_ACTION_RESTORED_USER_CHOICE]
        )
        self.assertEqual(target.read_bytes(), backup_payload)

    def test_unknown_choice_falls_back_to_keep(self):
        payload = b'{"theme": "dark"}'
        self._backup_file("settings.json", payload)
        (self.source_root / "settings.json").write_bytes(b"user")
        self._manifest([self._entry("settings.json", payload)])

        outcome = restore_configs(str(self.backup_dir), choice="overwrite")

        self.assertEqual(self._actions(outcome), [CONFIG_ACTION_KEPT_USER])

    # -- case D ----------------------------------------------------------

    def test_missing_backup_directory_is_unavailable(self):
        outcome = restore_configs("")

        self.assertFalse(outcome.ok)
        self.assertFalse(outcome.complete)
        self.assertEqual(outcome.status, CONFIG_STATUS_UNAVAILABLE)
        self.assertTrue(outcome.reason)

    def test_missing_manifest_is_unavailable(self):
        outcome = restore_configs(str(self.backup_dir))

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.status, CONFIG_STATUS_UNAVAILABLE)
        self.assertIn("manifest", outcome.reason)

    def test_corrupt_manifest_is_unavailable(self):
        (self.backup_dir / "manifest.json").write_text("{not json", encoding="utf-8")

        outcome = restore_configs(str(self.backup_dir))

        self.assertEqual(outcome.status, CONFIG_STATUS_UNAVAILABLE)
        self.assertFalse(outcome.complete)

    def test_missing_backup_file_is_reported(self):
        payload = b'{"theme": "dark"}'
        self._manifest([self._entry("settings.json", payload)])

        outcome = restore_configs(str(self.backup_dir))

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.status, CONFIG_STATUS_FAILED)
        self.assertEqual(outcome.unavailable, ["settings.json"])
        self.assertEqual(self._actions(outcome), [CONFIG_ACTION_BACKUP_MISSING])
        self.assertFalse((self.source_root / "settings.json").exists())

    def test_checksum_mismatch_is_reported_as_corrupt(self):
        payload = b'{"theme": "dark"}'
        tampered = b'{"theme": "evil"}'
        self._backup_file("settings.json", tampered)
        self._manifest([self._entry("settings.json", payload)])

        outcome = restore_configs(str(self.backup_dir))

        self.assertEqual(outcome.status, CONFIG_STATUS_FAILED)
        self.assertEqual(self._actions(outcome), [CONFIG_ACTION_BACKUP_CORRUPT])
        self.assertFalse((self.source_root / "settings.json").exists())

    def test_size_mismatch_is_reported_as_corrupt(self):
        payload = b'{"theme": "dark"}'
        self._backup_file("settings.json", payload)
        self._manifest([self._entry("settings.json", payload, size=len(payload) + 1)])

        outcome = restore_configs(str(self.backup_dir))

        self.assertEqual(self._actions(outcome), [CONFIG_ACTION_BACKUP_CORRUPT])

    # -- unsafe manifests ------------------------------------------------

    def test_unsafe_relative_paths_are_rejected(self):
        unsafe = ("../evil.txt", "/etc/passwd", "C:/evil.txt", "", ".", "a/../b")
        entries = [self._entry(rel, b"x") for rel in unsafe]
        self._manifest(entries)

        outcome = restore_configs(str(self.backup_dir))

        self.assertEqual(outcome.status, CONFIG_STATUS_FAILED)
        self.assertFalse(outcome.complete)
        self.assertEqual(
            self._actions(outcome), [CONFIG_ACTION_BACKUP_CORRUPT] * len(unsafe)
        )
        self.assertFalse((self.source_root.parent / "evil.txt").exists())

    def test_non_mapping_entry_is_corrupt(self):
        self._manifest(["settings.json"])

        outcome = restore_configs(str(self.backup_dir))

        self.assertEqual(outcome.status, CONFIG_STATUS_FAILED)
        self.assertEqual(self._actions(outcome), [CONFIG_ACTION_BACKUP_CORRUPT])

    def test_tool_mismatch_is_unavailable(self):
        payload = b"x"
        self._backup_file("settings.json", payload)
        self._manifest([self._entry("settings.json", payload)])

        outcome = restore_configs(str(self.backup_dir), expected_tool_id="other")

        self.assertEqual(outcome.status, CONFIG_STATUS_UNAVAILABLE)
        self.assertFalse(outcome.complete)

    def test_job_mismatch_is_unavailable(self):
        payload = b"x"
        self._backup_file("settings.json", payload)
        self._manifest([self._entry("settings.json", payload)])

        outcome = restore_configs(str(self.backup_dir), expected_job_id="deadbeef")

        self.assertEqual(outcome.status, CONFIG_STATUS_UNAVAILABLE)

    def test_missing_source_root_is_unavailable(self):
        self._manifest([], source_root="")

        outcome = restore_configs(str(self.backup_dir))

        self.assertEqual(outcome.status, CONFIG_STATUS_UNAVAILABLE)

    def test_non_list_entries_is_unavailable(self):
        self._manifest(entries="settings.json")

        outcome = restore_configs(str(self.backup_dir))

        self.assertEqual(outcome.status, CONFIG_STATUS_UNAVAILABLE)

    # -- aggregate statuses ----------------------------------------------

    def test_empty_entries_need_no_restore(self):
        self._manifest([])

        outcome = restore_configs(str(self.backup_dir))

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.status, CONFIG_STATUS_NOT_REQUIRED)

    def test_partial_status_when_one_file_fails(self):
        payload = b'{"theme": "dark"}'
        self._backup_file("settings.json", payload)
        self._manifest(
            [self._entry("settings.json", payload), self._entry("argv.json", b"{}")]
        )

        outcome = restore_configs(str(self.backup_dir))

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.status, CONFIG_STATUS_PARTIAL)
        self.assertEqual(outcome.restored, ["settings.json"])
        self.assertEqual(outcome.unavailable, ["argv.json"])

    def test_write_failure_keeps_the_existing_path(self):
        payload = b'{"theme": "dark"}'
        self._backup_file("settings.json", payload)
        blocked = self.source_root / "settings.json"
        blocked.mkdir()
        (blocked / "keep.txt").write_text("keep", encoding="utf-8")
        self._manifest([self._entry("settings.json", payload)])

        outcome = restore_configs(str(self.backup_dir))

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.status, CONFIG_STATUS_FAILED)
        self.assertEqual(outcome.failed, ["settings.json"])
        self.assertEqual(self._actions(outcome), [CONFIG_ACTION_RESTORE_FAILED])
        self.assertTrue(blocked.is_dir())
        self.assertTrue((blocked / "keep.txt").is_file())
        self.assertEqual(
            sorted(os.listdir(str(self.source_root))), ["settings.json"]
        )


if __name__ == "__main__":
    unittest.main()
