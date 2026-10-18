# -*- coding: utf-8 -*-
"""Unit tests for migration engine A (backup -> copy -> verify).

The transfer primitive is injected so the tests never depend on robocopy;
tools/migrate_drill.py exercises the real one.
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.migrate import (  # noqa: E402
    ERROR_BACKUP,
    ERROR_COPY,
    ERROR_PREPARE,
    ERROR_VERIFY,
    MigrationCancelled,
    MigrationRequest,
    prepare_copy,
)
from src.core.scan.cancel import CancelToken  # noqa: E402
from src.platform import TransferCancelled  # noqa: E402


class EngineTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory(prefix="dw-t09-engine-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.tool_root = self.root / "Cursor"
        self.source = self.tool_root / "Cache"
        self.source.mkdir(parents=True)
        (self.tool_root / "User").mkdir()
        (self.tool_root / "User" / "settings.json").write_text(
            '{"theme": "dark"}', encoding="utf-8"
        )
        self.write(self.source, "a.bin", b"a" * 1000)
        self.write(self.source, "sub/b.bin", b"b" * 2048)
        (self.source / "empty").mkdir()
        self.target_root = self.root / "target" / "data"
        self.target_root.mkdir(parents=True)
        self.backup_root = self.root / "target" / "backup"

    def write(self, base, rel, payload):
        path = base.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def request(self, **overrides):
        values = dict(
            tool_id="demo",
            source_path=str(self.source),
            target_root=str(self.target_root),
            backup_root=str(self.backup_root),
            config_root=str(self.tool_root),
        )
        values.update(overrides)
        return MigrationRequest(**values)

    def make_transfer(self, exit_code=0, mutate=None):
        def transfer(src, dst, log_path, cancel_token=None):
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            Path(log_path).write_text("fake robocopy\n", encoding="utf-8")
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
            if mutate is not None:
                mutate(Path(dst))
            return exit_code

        return transfer

    def staging_parents(self):
        tool_dir = self.target_root / "demo"
        if not tool_dir.exists():
            return []
        return [
            entry
            for entry in tool_dir.iterdir()
            if entry.name.startswith(".dw-partial-")
        ]

    def test_success_leaves_verified_staging_and_untouched_source(self):
        source_snapshot = {
            path.relative_to(self.source).as_posix(): path.read_bytes()
            for path in self.source.rglob("*")
            if path.is_file()
        }
        events = []

        result = prepare_copy(
            self.request(), transfer=self.make_transfer(), progress_cb=events.append
        )

        self.assertTrue(result.ok, result.error_message)
        self.assertEqual(result.error_code, None)
        self.assertTrue(result.verify.ok)
        self.assertEqual(result.verify.source_file_count, 2)
        self.assertEqual(result.verify.target_file_count, 2)
        self.assertEqual(result.verify.source_bytes, 3048)
        self.assertEqual(result.verify.target_bytes, 3048)
        self.assertTrue(os.path.isdir(result.staging_path))
        self.assertEqual(
            sorted(
                path.relative_to(result.staging_path).as_posix()
                for path in Path(result.staging_path).rglob("*")
                if path.is_file()
            ),
            ["a.bin", "sub/b.bin"],
        )
        self.assertTrue(os.path.isdir(Path(result.staging_path) / "empty"))

        # Source is byte-for-byte unchanged and still a real directory.
        self.assertFalse(os.path.islink(self.source))
        self.assertEqual(
            {
                path.relative_to(self.source).as_posix(): path.read_bytes()
                for path in self.source.rglob("*")
                if path.is_file()
            },
            source_snapshot,
        )

        manifest = result.backup
        self.assertIsNotNone(manifest)
        self.assertEqual(
            [entry.rel_path for entry in manifest.entries],
            ["User/settings.json"],
        )
        self.assertTrue(
            os.path.isfile(os.path.join(result.backup_dir, "manifest.json"))
        )
        self.assertTrue(os.path.isfile(os.path.join(
            os.path.dirname(result.staging_path), "robocopy.log"
        )))
        self.assertEqual(
            result.final_path,
            str(self.target_root / "demo" / "Cache"),
        )

        stages = [(event["stage"], event["status"]) for event in events]
        self.assertEqual(
            stages,
            [
                ("prepare", "start"),
                ("prepare", "done"),
                ("backup", "start"),
                ("backup", "done"),
                ("copy", "start"),
                ("copy", "done"),
                ("verify", "start"),
                ("verify", "done"),
            ],
        )

    def test_robocopy_exit_eight_aborts_and_removes_staging(self):
        result = prepare_copy(
            self.request(), transfer=self.make_transfer(exit_code=8)
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, ERROR_COPY)
        self.assertIn("robocopy exited with 8", result.error_message)
        self.assertTrue(result.temp_removed)
        self.assertEqual(self.staging_parents(), [])
        self.assertTrue(os.path.isdir(self.source))
        self.assertTrue(
            [entry for entry in self.backup_root.iterdir() if entry.is_dir()]
        )

    def test_verify_mismatch_aborts_and_removes_staging(self):
        def truncate(destination):
            with open(destination / "a.bin", "ab") as handle:
                handle.write(b"corruption")

        result = prepare_copy(
            self.request(), transfer=self.make_transfer(mutate=truncate)
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, ERROR_VERIFY)
        self.assertEqual(result.verify.size_mismatch_count, 1)
        self.assertEqual(result.verify.source_bytes, 3048)
        self.assertEqual(result.verify.target_bytes, 3058)
        self.assertIn("total bytes differ", result.error_message)
        self.assertTrue(result.temp_removed)
        self.assertEqual(self.staging_parents(), [])
        self.assertTrue(os.path.isdir(self.source))

    def test_verify_missing_file_aborts_and_removes_staging(self):
        def drop_file(destination):
            (destination / "sub" / "b.bin").unlink()

        result = prepare_copy(
            self.request(), transfer=self.make_transfer(mutate=drop_file)
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, ERROR_VERIFY)
        self.assertEqual(result.verify.missing_count, 1)
        self.assertEqual(result.verify.missing, ["sub/b.bin"])
        self.assertEqual(result.verify.source_file_count, 2)
        self.assertEqual(result.verify.target_file_count, 1)
        self.assertEqual(self.staging_parents(), [])

    def test_backup_failure_aborts_before_copy(self):
        calls = []

        def transfer(*args, **kwargs):
            calls.append(args)
            return 0

        result = prepare_copy(
            self.request(config_files=("missing-settings.json",)),
            transfer=transfer,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, ERROR_BACKUP)
        self.assertEqual(calls, [])
        self.assertTrue(result.temp_removed)
        self.assertFalse(self.backup_root.exists() and any(self.backup_root.iterdir()))
        self.assertEqual(self.staging_parents(), [])

    def test_cancellation_during_copy_removes_staging(self):
        token = CancelToken()

        def transfer(src, dst, log_path, cancel_token=None):
            Path(dst).mkdir(parents=True, exist_ok=True)
            (Path(dst) / "partial.bin").write_bytes(b"partial")
            cancel_token.cancel()
            raise TransferCancelled("cancelled by test")

        with self.assertRaises(MigrationCancelled):
            prepare_copy(self.request(), transfer=transfer, cancel_token=token)

        self.assertEqual(self.staging_parents(), [])
        self.assertTrue(os.path.isdir(self.source))

    def test_cancelled_token_before_copy_creates_nothing(self):
        token = CancelToken()
        token.cancel()

        def transfer(*args, **kwargs):
            raise AssertionError("transfer must not run")

        with self.assertRaises(MigrationCancelled):
            prepare_copy(self.request(), transfer=transfer, cancel_token=token)

        self.assertEqual(self.staging_parents(), [])
        self.assertFalse(self.backup_root.exists() and any(self.backup_root.iterdir()))

    def test_missing_source_is_a_prepare_failure(self):
        result = prepare_copy(
            self.request(source_path=str(self.root / "nope")),
            transfer=self.make_transfer(),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, ERROR_PREPARE)
        self.assertIn("does not exist", result.error_message)

    def test_target_inside_source_is_refused(self):
        result = prepare_copy(
            self.request(target_root=str(self.source / "inner")),
            transfer=self.make_transfer(),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, ERROR_PREPARE)

    def test_missing_config_root_is_a_prepare_failure(self):
        result = prepare_copy(
            self.request(config_root=str(self.root / "nope-config")),
            transfer=self.make_transfer(),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, ERROR_PREPARE)

    def test_existing_staging_directory_is_a_conflict(self):
        job_id = "abcdef0123456789abcdef0123456789"
        staging_parent = (
            self.target_root / "demo" / ".dw-partial-abcdef01"
        )
        staging_parent.mkdir(parents=True)

        result = prepare_copy(
            self.request(job_id=job_id), transfer=self.make_transfer()
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, ERROR_COPY)
        self.assertIn("already exists", result.error_message)


if __name__ == "__main__":
    unittest.main()
