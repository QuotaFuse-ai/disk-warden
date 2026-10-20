# -*- coding: utf-8 -*-
"""Unit tests for configuration restore (AC-ROL-06 cases A/B/C/D).

Every case runs against a temporary sandbox; no real user configuration is
read or written.  The manifest lives in a timestamp-prefixed per-job directory
(the layout the migration writes) and is looked up by the migration job id.
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

from src.core.rollback.config import (  # noqa: E402
    locate_manifest,
    restore_configs,
)
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
    CONFIG_STATUS_ERROR,
    CONFIG_STATUS_RESTORED,
    CONFIG_STATUS_SKIPPED,
)

JOB_ID = "0123456789abcdef"
JOB8 = "01234567"
OTHER_JOB_ID = "89abcdef01234567"
OTHER_JOB8 = "89abcdef"
JOB_DIR_NAME = "2026-10-20_161403_demo_" + JOB8


class ConfigRestoreTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory(prefix="dw-t11-config-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.source_root = self.root / "Cursor"
        self.source_root.mkdir()
        self.backup_root = self.root / "backup"
        self.backup_dir = self.backup_root / JOB_DIR_NAME
        (self.backup_dir / "files").mkdir(parents=True)

    # -- helpers ---------------------------------------------------------

    def _backup_file(self, rel, payload, backup_dir=None):
        path = Path(backup_dir or self.backup_dir) / "files" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def _manifest(self, entries=None, backup_dir=None, **overrides):
        payload = {
            "schema_version": 1,
            "tool_id": "demo",
            "job_id": JOB_ID,
            "source_root": str(self.source_root),
            "entries": entries if entries is not None else [],
        }
        payload.update(overrides)
        directory = Path(backup_dir or self.backup_dir)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "manifest.json").write_text(
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

    def _restore(self, backup_dir=None, choice=CONFIG_KEEP, **overrides):
        return restore_configs(
            str(backup_dir or self.backup_dir),
            choice=choice,
            expected_tool_id=overrides.pop("expected_tool_id", "demo"),
            expected_job_id=overrides.pop("expected_job_id", JOB_ID),
            **overrides
        )

    # -- locating the manifest (T12-F regression) ------------------------

    def test_manifest_is_found_in_timestamped_job_dir_under_backup_root(self):
        payload = b'{"extensions": []}'
        self._backup_file("extensions.json", payload)
        self._manifest([self._entry("extensions.json", payload)])

        outcome = restore_configs(
            str(self.backup_root),
            expected_tool_id="demo",
            expected_job_id=JOB_ID,
        )

        self.assertEqual(outcome.status, CONFIG_STATUS_RESTORED)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.restored, ["extensions.json"])
        self.assertEqual(outcome.backup_dir, str(self.backup_dir))
        self.assertEqual(
            outcome.manifest_path, str(self.backup_dir / "manifest.json")
        )
        self.assertEqual(
            (self.source_root / "extensions.json").read_bytes(), payload
        )
        self.assertEqual(
            outcome.checksums["extensions.json"],
            hashlib.sha256(payload).hexdigest(),
        )

    def test_job_dir_is_matched_by_manifest_job_id_not_by_name(self):
        payload = b'{"theme": "dark"}'
        renamed = self.backup_root / "my-restored-backup"
        self._backup_file("settings.json", payload, backup_dir=renamed)
        self._manifest([self._entry("settings.json", payload)], backup_dir=renamed)

        outcome = restore_configs(
            str(self.backup_root),
            expected_tool_id="demo",
            expected_job_id=JOB_ID,
        )

        self.assertEqual(outcome.status, CONFIG_STATUS_RESTORED)
        self.assertEqual(outcome.backup_dir, str(renamed))

    def test_backup_root_scan_picks_the_matching_job(self):
        payload = b'{"theme": "dark"}'
        mine = self.backup_root / JOB_DIR_NAME
        other = self.backup_root / ("2026-10-20_150000_demo_" + OTHER_JOB8)
        self._backup_file("settings.json", payload, backup_dir=mine)
        self._manifest([self._entry("settings.json", payload)], backup_dir=mine)
        self._backup_file("settings.json", b"other", backup_dir=other)
        self._manifest(
            [self._entry("settings.json", b"other")],
            backup_dir=other,
            job_id=OTHER_JOB_ID,
        )

        directory, manifest, error = locate_manifest(str(self.backup_root), JOB_ID)

        self.assertEqual(directory, str(mine))
        self.assertEqual(manifest["job_id"], JOB_ID)
        self.assertEqual(error, "")

    def test_manifest_for_another_job_is_an_error(self):
        payload = b"x"
        other = self.backup_root / ("2026-10-20_150000_demo_" + OTHER_JOB8)
        self._backup_file("settings.json", payload, backup_dir=other)
        self._manifest(
            [self._entry("settings.json", payload)],
            backup_dir=other,
            job_id=OTHER_JOB_ID,
        )

        outcome = self._restore(self.backup_root)

        self.assertEqual(outcome.status, CONFIG_STATUS_ERROR)
        self.assertFalse(outcome.ok)
        self.assertIn("belongs to job", outcome.reason)

    def test_manifest_truly_absent_is_an_error(self):
        outcome = self._restore(self.backup_root)

        self.assertEqual(outcome.status, CONFIG_STATUS_ERROR)
        self.assertFalse(outcome.complete)
        self.assertFalse(outcome.ok)
        self.assertIn("no configuration backup manifest", outcome.reason)
        self.assertIn(str(self.backup_root), outcome.reason)
        self.assertEqual(outcome.restored, [])

    def test_missing_backup_directory_is_an_error(self):
        outcome = self._restore(self.root / "does-not-exist")

        self.assertEqual(outcome.status, CONFIG_STATUS_ERROR)
        self.assertIn("no configuration backup manifest", outcome.reason)

    def test_corrupt_manifest_is_an_error(self):
        (self.backup_dir / "manifest.json").write_text(
            "{not json", encoding="utf-8"
        )

        outcome = self._restore()

        self.assertEqual(outcome.status, CONFIG_STATUS_ERROR)
        self.assertFalse(outcome.complete)
        self.assertIn("manifest", outcome.reason)
        self.assertIn("unreadable", outcome.reason)

    # -- case A ----------------------------------------------------------

    def test_identical_file_is_kept_without_rewrite(self):
        payload = b'{"theme": "dark"}'
        self._backup_file("settings.json", payload)
        target = self.source_root / "settings.json"
        target.write_bytes(payload)
        self._manifest([self._entry("settings.json", payload)])

        outcome = self._restore()

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.status, CONFIG_STATUS_SKIPPED)
        self.assertEqual(outcome.kept, ["settings.json"])
        self.assertEqual(outcome.restored, [])
        self.assertEqual(self._actions(outcome), [CONFIG_ACTION_IDENTICAL])
        self.assertEqual(target.read_bytes(), payload)

    # -- case B ----------------------------------------------------------

    def test_missing_current_file_is_restored(self):
        payload = b'{"theme": "dark"}'
        self._backup_file("settings.json", payload)
        self._manifest([self._entry("settings.json", payload)])

        outcome = self._restore()

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.status, CONFIG_STATUS_RESTORED)
        self.assertEqual(outcome.restored, ["settings.json"])
        self.assertEqual(self._actions(outcome), [CONFIG_ACTION_RESTORED_MISSING])
        self.assertEqual(
            (self.source_root / "settings.json").read_bytes(), payload
        )

    def test_restored_files_carry_their_sha256(self):
        payload = b'{"theme": "dark"}'
        self._backup_file("settings.json", payload)
        self._manifest([self._entry("settings.json", payload)])

        outcome = self._restore()

        digest = hashlib.sha256(payload).hexdigest()
        self.assertEqual(outcome.checksums, {"settings.json": digest})
        self.assertTrue(outcome.details[0]["verified"])
        self.assertEqual(outcome.details[0]["sha256"], digest)

    def test_nested_file_is_restored(self):
        payload = b'{"key": "value"}'
        self._backup_file("User/keybindings.json", payload)
        self._manifest([self._entry("User/keybindings.json", payload)])

        outcome = self._restore()

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

        outcome = self._restore(choice=CONFIG_KEEP)

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.status, CONFIG_STATUS_SKIPPED)
        self.assertEqual(self._actions(outcome), [CONFIG_ACTION_KEPT_USER])
        self.assertEqual(target.read_bytes(), user_payload)

    def test_explicit_restore_replaces_user_modified_file(self):
        backup_payload = b'{"theme": "dark"}'
        user_payload = b'{"theme": "light"}'
        self._backup_file("settings.json", backup_payload)
        target = self.source_root / "settings.json"
        target.write_bytes(user_payload)
        self._manifest([self._entry("settings.json", backup_payload)])

        outcome = self._restore(choice=CONFIG_RESTORE)

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

        outcome = self._restore(choice="overwrite")

        self.assertEqual(self._actions(outcome), [CONFIG_ACTION_KEPT_USER])
        self.assertEqual(outcome.status, CONFIG_STATUS_SKIPPED)

    # -- case D ----------------------------------------------------------

    def test_no_recorded_backup_is_skipped(self):
        outcome = restore_configs("")

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.status, CONFIG_STATUS_SKIPPED)
        self.assertIn("nothing to restore", outcome.reason)

    def test_missing_backup_file_is_reported_with_its_name(self):
        payload = b'{"theme": "dark"}'
        self._manifest([self._entry("settings.json", payload)])

        outcome = self._restore()

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.status, CONFIG_STATUS_ERROR)
        self.assertEqual(outcome.unavailable, ["settings.json"])
        self.assertEqual(self._actions(outcome), [CONFIG_ACTION_BACKUP_MISSING])
        self.assertIn("settings.json", outcome.reason)
        self.assertFalse((self.source_root / "settings.json").exists())

    def test_checksum_mismatch_is_reported_as_corrupt(self):
        payload = b'{"theme": "dark"}'
        tampered = b'{"theme": "evil"}'
        self._backup_file("settings.json", tampered)
        self._manifest([self._entry("settings.json", payload)])

        outcome = self._restore()

        self.assertEqual(outcome.status, CONFIG_STATUS_ERROR)
        self.assertEqual(self._actions(outcome), [CONFIG_ACTION_BACKUP_CORRUPT])
        self.assertIn("settings.json", outcome.reason)
        self.assertFalse((self.source_root / "settings.json").exists())

    def test_size_mismatch_is_reported_as_corrupt(self):
        payload = b'{"theme": "dark"}'
        self._backup_file("settings.json", payload)
        self._manifest([self._entry("settings.json", payload, size=len(payload) + 1)])

        outcome = self._restore()

        self.assertEqual(self._actions(outcome), [CONFIG_ACTION_BACKUP_CORRUPT])

    # -- unsafe manifests ------------------------------------------------

    def test_unsafe_relative_paths_are_rejected(self):
        unsafe = ["../evil.txt", "/etc/passwd", "", ".", "a/../b"]
        if os.name == "nt":
            # A drive-qualified path only means "absolute" on Windows.  On
            # POSIX ``C:`` is an ordinary file name, so that entry is a
            # missing backup instead of a corrupt manifest record.
            unsafe.insert(2, "C:/evil.txt")
        entries = [self._entry(rel, b"x") for rel in unsafe]
        self._manifest(entries)

        outcome = self._restore()

        self.assertEqual(outcome.status, CONFIG_STATUS_ERROR)
        self.assertFalse(outcome.complete)
        self.assertEqual(
            self._actions(outcome), [CONFIG_ACTION_BACKUP_CORRUPT] * len(unsafe)
        )
        self.assertFalse((self.source_root.parent / "evil.txt").exists())

    def test_non_mapping_entry_is_corrupt(self):
        self._manifest(["settings.json"])

        outcome = self._restore()

        self.assertEqual(outcome.status, CONFIG_STATUS_ERROR)
        self.assertEqual(self._actions(outcome), [CONFIG_ACTION_BACKUP_CORRUPT])

    def test_tool_mismatch_is_an_error(self):
        payload = b"x"
        self._backup_file("settings.json", payload)
        self._manifest([self._entry("settings.json", payload)])

        outcome = self._restore(expected_tool_id="other")

        self.assertEqual(outcome.status, CONFIG_STATUS_ERROR)
        self.assertIn("belongs to tool", outcome.reason)

    def test_job_mismatch_is_an_error(self):
        payload = b"x"
        self._backup_file("settings.json", payload)
        self._manifest([self._entry("settings.json", payload)])

        outcome = self._restore(expected_job_id="deadbeef")

        self.assertEqual(outcome.status, CONFIG_STATUS_ERROR)

    def test_missing_source_root_is_an_error(self):
        self._manifest([], source_root="")

        outcome = self._restore()

        self.assertEqual(outcome.status, CONFIG_STATUS_ERROR)
        self.assertIn("source root", outcome.reason)

    def test_non_list_entries_is_an_error(self):
        self._manifest(entries="settings.json")

        outcome = self._restore()

        self.assertEqual(outcome.status, CONFIG_STATUS_ERROR)

    # -- aggregate statuses ----------------------------------------------

    def test_empty_entries_is_skipped(self):
        self._manifest([])

        outcome = self._restore()

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.status, CONFIG_STATUS_SKIPPED)
        self.assertIn("did not change any configuration file", outcome.reason)
        self.assertEqual(outcome.restored, [])

    def test_partial_failure_is_an_error_naming_the_file(self):
        payload = b'{"theme": "dark"}'
        self._backup_file("settings.json", payload)
        self._manifest(
            [self._entry("settings.json", payload), self._entry("argv.json", b"{}")]
        )

        outcome = self._restore()

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.status, CONFIG_STATUS_ERROR)
        self.assertEqual(outcome.restored, ["settings.json"])
        self.assertEqual(outcome.unavailable, ["argv.json"])
        self.assertIn("argv.json", outcome.reason)

    def test_write_failure_keeps_the_existing_path(self):
        payload = b'{"theme": "dark"}'
        self._backup_file("settings.json", payload)
        blocked = self.source_root / "settings.json"
        blocked.mkdir()
        (blocked / "keep.txt").write_text("keep", encoding="utf-8")
        self._manifest([self._entry("settings.json", payload)])

        outcome = self._restore()

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.status, CONFIG_STATUS_ERROR)
        self.assertEqual(outcome.failed, ["settings.json"])
        self.assertEqual(self._actions(outcome), [CONFIG_ACTION_RESTORE_FAILED])
        self.assertIn("settings.json", outcome.reason)
        self.assertTrue(blocked.is_dir())
        self.assertTrue((blocked / "keep.txt").is_file())
        self.assertEqual(
            sorted(os.listdir(str(self.source_root))), ["settings.json"]
        )


if __name__ == "__main__":
    unittest.main()
