# -*- coding: utf-8 -*-
"""Unit tests for migration engine B (S5-S9: promote, swap, link, cleanup).

Junctions are simulated by ``FakeOps``; the real ``mklink /J`` path is covered
by ``tools/migrate_drill_b.py``.  Every failure case also asserts that the
source data is still where the architecture says it must be.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.migrate import (  # noqa: E402
    ERROR_CANCELLED,
    ERROR_CLEANUP,
    ERROR_INPUT,
    ERROR_LINK,
    ERROR_LINK_VERIFY,
    ERROR_PROCESS_BUSY,
    ERROR_SWAP,
    STATE_DONE,
    STATE_FAILED,
    STATE_LINKED_CLEANUP_PENDING,
    STATE_RECOVERY_REQUIRED,
    STATE_ROLLED_BACK,
    complete_migration,
    marker_path,
    recover_job,
)
from src.core.migrate import complete as complete_module  # noqa: E402
from src.core.scan.cancel import CancelToken  # noqa: E402
from src.platform import link_target_matches  # noqa: E402
from tests.migrate_fakes import (  # noqa: E402
    FakeOps,
    MigrationFixture,
    Process,
    expected_snapshot,
    snapshot,
)


class CompleteTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory(
            prefix="dw-t10-complete-"
        )
        self.addCleanup(self._temporary.cleanup)
        self.fixture = MigrationFixture(Path(self._temporary.name))
        self.fixture.make_staging()
        self.ops = FakeOps()
        self.events = []

    def run_complete(self, names=(), provider=None, token=None, result=None):
        return complete_migration(
            result or self.fixture.make_result(),
            process_names=names,
            process_provider=provider or (lambda: []),
            ops=self.ops,
            progress_cb=self.events.append,
            cancel_token=token,
        )

    def assert_source_tree(self):
        self.assertTrue(os.path.isdir(str(self.fixture.source)))
        self.assertFalse(self.ops.is_link(str(self.fixture.source)))
        self.assertEqual(snapshot(self.fixture.source), expected_snapshot())

    def test_success_links_verifies_and_cleans(self):
        result = self.run_complete(names=("demo.exe",))

        self.assertTrue(result.ok, result.error_message)
        self.assertEqual(result.state, STATE_DONE)
        self.assertTrue(result.link_created)
        self.assertTrue(result.link_verified)
        self.assertTrue(result.source_removed)
        self.assertEqual(result.cleanup_errors, [])
        self.assertIsNone(result.error_code)

        self.assertTrue(self.ops.is_junction(str(self.fixture.source)))
        self.assertTrue(
            link_target_matches(
                self.ops.read_link_target(str(self.fixture.source)),
                str(self.fixture.final),
            )
        )
        self.assertEqual(
            self.ops.read_bytes(str(self.fixture.source / "sub" / "b.bin"), 4),
            b"bbbb",
        )
        self.assertTrue(self.fixture.final.is_dir())
        self.assertTrue(marker_path(str(self.fixture.final), self.fixture.job_id))
        self.assertFalse(self.fixture.tombstone.exists())
        self.assertFalse(self.fixture.staging_parent.exists())

        self.assertEqual(result.process["status"], "not_running")
        step_names = [step["step"] for step in result.steps]
        for name in ("S5", "S6", "S7", "S8", "S9"):
            self.assertIn(name, step_names)

    def test_progress_events_cover_link_and_verify(self):
        self.run_complete()

        seen = [(event["stage"], event["status"]) for event in self.events]
        for expected in (
            ("swap", "start"),
            ("swap", "progress"),
            ("swap", "done"),
            ("link", "start"),
            ("link", "done"),
            ("link_verify", "start"),
            ("link_verify", "done"),
            ("cleanup", "start"),
            ("cleanup", "done"),
        ):
            self.assertIn(expected, seen)
        self.assertLess(seen.index(("swap", "done")), seen.index(("link", "start")))
        self.assertLess(
            seen.index(("link_verify", "done")),
            seen.index(("cleanup", "start")),
        )
        for event in self.events:
            self.assertEqual(event["job_id"], self.fixture.job_id)
            self.assertEqual(event["tool_id"], self.fixture.tool_id)
            self.assertIn("elapsed_seconds", event)
        self.assertEqual(self.events[-1]["state"], STATE_DONE)

    def test_busy_process_keeps_source_and_removes_staging(self):
        busy = Process(4321, "demo.exe", "C:\\Apps\\demo.exe")

        result = self.run_complete(
            names=("demo.exe",), provider=lambda: [busy]
        )

        self.assertEqual(result.state, STATE_FAILED)
        self.assertEqual(result.error_code, ERROR_PROCESS_BUSY)
        self.assertTrue(result.process["blocked_by_process"])
        self.assert_source_tree()
        self.assertFalse(self.fixture.staging_parent.exists())
        self.assertFalse(self.fixture.final.exists())
        self.assertFalse(self.fixture.tombstone.exists())

    def test_unknown_process_state_blocks(self):
        def broken():
            raise RuntimeError("process list unavailable")

        result = self.run_complete(names=("demo.exe",), provider=broken)

        self.assertEqual(result.error_code, ERROR_PROCESS_BUSY)
        self.assertEqual(result.process["status"], "unknown")
        self.assert_source_tree()

    def test_unverified_result_is_rejected_without_touching_source(self):
        result = self.run_complete(result=self.fixture.make_result(ok=False))

        self.assertEqual(result.state, STATE_FAILED)
        self.assertEqual(result.error_code, ERROR_INPUT)
        self.assert_source_tree()
        self.assertTrue(self.fixture.staging_parent.exists())

    def test_existing_target_without_marker_is_refused(self):
        self.fixture.make_final(owned=False)

        result = self.run_complete()

        self.assertEqual(result.state, STATE_FAILED)
        self.assertEqual(result.error_code, ERROR_SWAP)
        self.assert_source_tree()
        self.assertTrue(self.fixture.final.is_dir())
        self.assertFalse(self.fixture.staging_parent.exists())

    def test_link_creation_failure_restores_source_and_removes_target(self):
        self.ops.fail_create_link = "junction creation failed"

        result = self.run_complete()

        self.assertEqual(result.state, STATE_ROLLED_BACK)
        self.assertTrue(result.rolled_back)
        self.assertEqual(result.error_code, ERROR_LINK)
        self.assertFalse(result.link_created)
        self.assert_source_tree()
        self.assertFalse(self.fixture.final.exists())
        self.assertFalse(self.fixture.tombstone.exists())
        self.assertFalse(self.fixture.staging_parent.exists())

    def test_source_rename_failure_restores_pre_migration_state(self):
        self.ops.fail_rename = (
            lambda source, destination: str(source) == str(self.fixture.source)
        )

        result = self.run_complete()

        self.assertEqual(result.state, STATE_ROLLED_BACK)
        self.assertTrue(result.rolled_back)
        self.assertEqual(result.error_code, ERROR_SWAP)
        self.assert_source_tree()
        self.assertFalse(self.fixture.final.exists())
        self.assertFalse(self.fixture.tombstone.exists())
        self.assertFalse(self.fixture.staging_parent.exists())

    def test_link_verification_failure_restores_source_and_keeps_target(self):
        # S8 reads one byte through the junction; the fake returns different
        # bytes than the direct read.
        self.ops.break_link_reads = True

        result = self.run_complete()

        self.assertEqual(result.state, STATE_ROLLED_BACK)
        self.assertEqual(result.error_code, ERROR_LINK_VERIFY)
        self.assertFalse(result.link_verified)
        self.assert_source_tree()
        self.assertFalse(self.ops.is_link(str(self.fixture.source)))
        self.assertFalse(self.fixture.tombstone.exists())
        # Architecture 6.3: the S8 rollback keeps the verified target.
        self.assertTrue(self.fixture.final.is_dir())
        self.assertTrue(
            marker_path(str(self.fixture.final), self.fixture.job_id)
        )
        self.assertFalse(self.fixture.staging_parent.exists())

    def test_cleanup_failure_is_pending_and_recovery_finishes(self):
        with mock.patch.object(
            complete_module, "remove_tombstone", return_value=["file locked"]
        ):
            result = self.run_complete()

        self.assertEqual(result.state, STATE_LINKED_CLEANUP_PENDING)
        self.assertTrue(result.ok)
        self.assertTrue(result.cleanup_pending)
        self.assertEqual(result.error_code, ERROR_CLEANUP)
        self.assertTrue(self.ops.is_junction(str(self.fixture.source)))
        self.assertTrue(self.fixture.tombstone.is_dir())

        recovered = recover_job(
            self.fixture.job_id,
            self.fixture.tool_id,
            str(self.fixture.source),
            str(self.fixture.final),
            staging_path=str(self.fixture.staging),
            recorded_state=STATE_LINKED_CLEANUP_PENDING,
            ops=self.ops,
        )

        self.assertEqual(recovered.state, STATE_DONE)
        self.assertTrue(recovered.link_verified)
        self.assertFalse(self.fixture.tombstone.exists())
        self.assertTrue(self.ops.is_junction(str(self.fixture.source)))

    def test_cancel_before_promote_removes_staging_only(self):
        token = CancelToken()
        token.cancel()

        result = self.run_complete(token=token)

        self.assertEqual(result.state, STATE_FAILED)
        self.assertEqual(result.error_code, ERROR_CANCELLED)
        self.assert_source_tree()
        self.assertFalse(self.fixture.staging_parent.exists())
        self.assertFalse(self.fixture.final.exists())


if __name__ == "__main__":
    unittest.main()
