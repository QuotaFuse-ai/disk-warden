# -*- coding: utf-8 -*-
"""Recovery-matrix tests for the swap half (architecture 6.4).

Each case sets up one crash snapshot, runs :func:`recover_job`, and asserts
both the terminal state and that nothing outside the job's own paths was
touched.  Running recovery twice must be a no-op.
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
    ERROR_RECOVERY,
    STATE_COPIED,
    STATE_COPYING,
    STATE_COPY_VERIFIED,
    STATE_DONE,
    STATE_FAILED,
    STATE_LINKED,
    STATE_LINKED_CLEANUP_PENDING,
    STATE_RECOVERY_REQUIRED,
    STATE_ROLLED_BACK,
    STATE_SOURCE_RENAMED,
    STATE_TARGET_READY,
    recover_job,
)
from src.platform import link_target_matches  # noqa: E402
from tests.migrate_fakes import (  # noqa: E402
    FakeOps,
    MigrationFixture,
    expected_snapshot,
    snapshot,
)


class RecoverTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory(
            prefix="dw-t10-recover-"
        )
        self.addCleanup(self._temporary.cleanup)
        self.fixture = MigrationFixture(Path(self._temporary.name))
        self.ops = FakeOps()

    def recover(self, recorded_state):
        return recover_job(
            self.fixture.job_id,
            self.fixture.tool_id,
            str(self.fixture.source),
            str(self.fixture.final),
            staging_path=str(self.fixture.staging),
            recorded_state=recorded_state,
            ops=self.ops,
        )

    def add_link(self, target=None):
        target = target or self.fixture.final
        self.ops.links[FakeOps._key(str(self.fixture.source))] = str(target)
        return target

    def assert_source_tree(self):
        self.assertTrue(self.fixture.source.is_dir())
        self.assertFalse(self.ops.is_link(str(self.fixture.source)))
        self.assertEqual(snapshot(self.fixture.source), expected_snapshot())

    # -- pre-swap states -------------------------------------------------

    def test_copying_removes_staging_and_keeps_source(self):
        self.fixture.make_staging()

        result = self.recover(STATE_COPYING)

        self.assertEqual(result.state, STATE_FAILED)
        self.assert_source_tree()
        self.assertFalse(self.fixture.staging_parent.exists())
        self.assertFalse(self.fixture.final.exists())

        again = self.recover(STATE_COPYING)
        self.assertEqual(again.state, STATE_FAILED)
        self.assert_source_tree()

    def test_copied_without_staging_is_terminal(self):
        result = self.recover(STATE_COPIED)

        self.assertEqual(result.state, STATE_FAILED)
        self.assert_source_tree()

    def test_copy_verified_without_target_is_terminal(self):
        self.fixture.make_staging()

        result = self.recover(STATE_COPY_VERIFIED)

        self.assertEqual(result.state, STATE_FAILED)
        self.assert_source_tree()
        self.assertFalse(self.fixture.staging_parent.exists())

    def test_target_ready_removes_owned_target(self):
        self.fixture.make_staging()
        self.fixture.make_final()

        result = self.recover(STATE_TARGET_READY)

        self.assertEqual(result.state, STATE_FAILED)
        self.assert_source_tree()
        self.assertFalse(self.fixture.final.exists())
        self.assertFalse(self.fixture.staging_parent.exists())

    def test_target_ready_without_target_requires_recovery(self):
        result = self.recover(STATE_TARGET_READY)

        self.assertEqual(result.state, STATE_RECOVERY_REQUIRED)
        self.assertEqual(result.error_code, ERROR_RECOVERY)
        self.assert_source_tree()

    def test_target_without_marker_is_not_deleted(self):
        self.fixture.make_final(owned=False)

        result = self.recover(STATE_TARGET_READY)

        self.assertEqual(result.state, STATE_RECOVERY_REQUIRED)
        self.assert_source_tree()
        self.assertTrue(self.fixture.final.is_dir())
        self.assertEqual(snapshot(self.fixture.final), expected_snapshot())

    # -- SOURCE_RENAMED --------------------------------------------------

    def test_source_renamed_restores_source_and_removes_target(self):
        self.fixture.move_source_to_tombstone()
        self.fixture.make_final()
        self.assertTrue(self.fixture.tombstone.is_dir())

        result = self.recover(STATE_SOURCE_RENAMED)

        self.assertEqual(result.state, STATE_ROLLED_BACK)
        self.assertTrue(result.rolled_back)
        self.assert_source_tree()
        self.assertFalse(self.fixture.tombstone.exists())
        self.assertFalse(self.fixture.final.exists())

        again = self.recover(STATE_SOURCE_RENAMED)
        self.assertEqual(again.state, STATE_ROLLED_BACK)
        self.assert_source_tree()

    def test_source_renamed_with_link_is_completed(self):
        self.fixture.move_source_to_tombstone()
        self.fixture.make_final()
        self.add_link()

        result = self.recover(STATE_SOURCE_RENAMED)

        self.assertEqual(result.state, STATE_DONE)
        self.assertTrue(result.link_verified)
        self.assertTrue(result.source_removed)
        self.assertFalse(self.fixture.tombstone.exists())
        self.assertTrue(self.fixture.final.is_dir())
        self.assertTrue(self.ops.is_junction(str(self.fixture.source)))

        again = self.recover(STATE_LINKED)
        self.assertEqual(again.state, STATE_DONE)
        self.assertTrue(self.ops.is_junction(str(self.fixture.source)))

    def test_source_and_tombstone_both_present_requires_recovery(self):
        self.fixture.make_final()
        shutil.copytree(str(self.fixture.source), str(self.fixture.tombstone))

        result = self.recover(STATE_SOURCE_RENAMED)

        self.assertEqual(result.state, STATE_RECOVERY_REQUIRED)
        self.assert_source_tree()
        self.assertTrue(self.fixture.tombstone.is_dir())

    def test_tombstone_is_not_a_directory_requires_recovery(self):
        self.fixture.tombstone.write_bytes(b"not a directory")

        result = self.recover(STATE_SOURCE_RENAMED)

        self.assertEqual(result.state, STATE_RECOVERY_REQUIRED)
        self.assert_source_tree()
        self.assertTrue(self.fixture.tombstone.is_file())

    # -- link states -----------------------------------------------------

    def test_wrong_link_target_requires_recovery(self):
        elsewhere = self.fixture.root / "elsewhere"
        elsewhere.mkdir()
        self.fixture.make_final()
        self.fixture.move_source_to_tombstone()
        self.add_link(elsewhere)

        result = self.recover(STATE_LINKED)

        self.assertEqual(result.state, STATE_RECOVERY_REQUIRED)
        self.assertEqual(
            self.ops.read_link_target(str(self.fixture.source)),
            str(elsewhere),
        )
        self.assertTrue(self.fixture.final.is_dir())
        self.assertTrue(self.fixture.tombstone.is_dir())

    def test_link_verify_failure_with_tombstone_restores_source(self):
        self.fixture.make_final()
        self.fixture.move_source_to_tombstone()
        self.add_link()
        self.ops.break_link_reads = True

        result = self.recover(STATE_LINKED)

        self.assertEqual(result.state, STATE_ROLLED_BACK)
        self.assertFalse(result.link_verified)
        self.assert_source_tree()
        self.assertFalse(self.ops.is_link(str(self.fixture.source)))
        self.assertFalse(self.fixture.tombstone.exists())
        # S8 rollback leaves the verified target in place (architecture 6.3).
        self.assertTrue(self.fixture.final.is_dir())

        again = self.recover(STATE_ROLLED_BACK)
        self.assertEqual(again.state, STATE_ROLLED_BACK)
        self.assert_source_tree()

    def test_link_verify_failure_without_tombstone_requires_recovery(self):
        self.fixture.make_final()
        self.ops.links[FakeOps._key(str(self.fixture.source))] = str(
            self.fixture.final
        )
        self.ops.break_link_reads = True

        result = self.recover(STATE_LINKED)

        self.assertEqual(result.state, STATE_RECOVERY_REQUIRED)
        self.assertTrue(self.ops.is_junction(str(self.fixture.source)))
        self.assertTrue(self.fixture.final.is_dir())

    def test_cleanup_pending_is_finished(self):
        self.fixture.make_final()
        self.fixture.move_source_to_tombstone()
        self.add_link()

        result = self.recover(STATE_LINKED_CLEANUP_PENDING)

        self.assertEqual(result.state, STATE_DONE)
        self.assertFalse(self.fixture.tombstone.exists())

        again = self.recover(STATE_DONE)
        self.assertEqual(again.state, STATE_DONE)

    def test_done_state_with_plain_source_requires_recovery(self):
        self.fixture.make_final()

        result = self.recover(STATE_DONE)

        self.assertEqual(result.state, STATE_RECOVERY_REQUIRED)
        self.assert_source_tree()
        self.assertTrue(self.fixture.final.is_dir())

    # -- no evidence -----------------------------------------------------

    def test_all_paths_absent_requires_recovery(self):
        self.fixture.make_final()
        self.fixture.move_source_to_tombstone()
        shutil.rmtree(str(self.fixture.tombstone))

        result = self.recover(STATE_SOURCE_RENAMED)

        self.assertEqual(result.state, STATE_RECOVERY_REQUIRED)
        self.assertFalse(self.fixture.source.exists())
        self.assertFalse(self.fixture.tombstone.exists())
        self.assertTrue(self.fixture.final.is_dir())

    def test_link_target_must_match_final_path(self):
        self.fixture.make_final()
        self.fixture.move_source_to_tombstone()
        self.add_link()
        self.assertTrue(
            link_target_matches(
                self.ops.read_link_target(str(self.fixture.source)),
                str(self.fixture.final),
            )
        )


if __name__ == "__main__":
    unittest.main()
