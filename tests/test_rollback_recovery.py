# -*- coding: utf-8 -*-
"""Unit tests for interrupted-rollback recovery (AC-ROL-04, architecture 6.4).

Recovery is observation-driven: the filesystem state plus the journal state
decide whether to promote, re-link or ask for a human.  Junctions are
simulated in memory by ``FakeOps``; every file operation is real.
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

from src.core.migrate import paths as migrate_paths  # noqa: E402
from src.core.rollback.models import (  # noqa: E402
    ERROR_RECOVERY,
    STATE_ROLLBACK_FAILED,
    STATE_ROLLBACK_RECOVERY_REQUIRED,
    STATE_ROLLED_BACK,
)
from src.core.rollback.recovery import recover_rollback  # noqa: E402
from src.core.rules import item_id_for  # noqa: E402
from src.core.store.history import (  # noqa: E402
    RESULT_ROLLED_BACK,
    TYPE_ROLLBACK,
    HistoryStore,
)
from src.core.store.journal import (  # noqa: E402
    STATE_ROLLBACK_COPIED,
    STATE_ROLLBACK_CONFIG_RESTORED,
    STATE_ROLLBACK_COPYING,
    STATE_ROLLBACK_LINK_REMOVED,
    STATE_ROLLBACK_PLANNED,
    STATE_ROLLBACK_SOURCE_READY,
    STATE_ROLLBACK_VERIFIED,
    STATE_ROLLED_BACK as JOURNAL_STATE_ROLLED_BACK,
    TYPE_ROLLBACK as JOURNAL_TYPE_ROLLBACK,
    JournalStore,
)
from src.core.store.learned import SAFETY_RED, LearnedRulesStore  # noqa: E402
from tests.migrate_fakes import (  # noqa: E402
    JOB8,
    JOB_ID,
    FakeOps,
    MigrationFixture,
    expected_snapshot,
    snapshot,
)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory(prefix="dw-t11-recovery-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.fixture = MigrationFixture(self.root)
        self.source = self.fixture.source
        self.source_parent = self.fixture.source_parent
        self.final = self.fixture.final
        self.ops = FakeOps()
        self.config_dir = self.root / "config"
        self.jobs_dir = self.config_dir / "migration-jobs"
        self.jobs_dir.mkdir(parents=True)
        self.history_path = self.config_dir / "history.json"
        self.learned_path = self.config_dir / "rules-learned.json"
        self.restore_parent = Path(
            migrate_paths.restore_path(str(self.source), JOB_ID)
        )
        self.restore_child = self.restore_parent / self.source.name

    # -- helpers ---------------------------------------------------------

    def _journal(self, state, **overrides):
        values = dict(
            tool_id="demo",
            source=str(self.source),
            final=str(self.final),
            state=state,
        )
        values.update(overrides)
        return JournalStore(str(self.jobs_dir)).begin(
            JOB_ID, entry_type=JOURNAL_TYPE_ROLLBACK, **values
        )

    def _recover(self, **overrides):
        overrides.setdefault("jobs_dir", str(self.jobs_dir))
        overrides.setdefault("history_path", str(self.history_path))
        overrides.setdefault("learned_path", str(self.learned_path))
        return recover_rollback(
            JOB_ID,
            "demo",
            str(self.source),
            str(self.final),
            ops=self.ops,
            **overrides
        )

    def _make_restore_child(self):
        shutil.copytree(str(self.final), str(self.restore_child))
        marker = self.restore_child / migrate_paths.owner_marker_name(JOB_ID)
        if marker.exists():
            os.unlink(str(marker))
        return self.restore_child

    def _remove_source(self):
        shutil.rmtree(str(self.source))

    # -- promote a verified restore copy ---------------------------------

    def test_verified_restore_copy_is_promoted(self):
        self.fixture.make_final(owned=True)
        self._remove_source()
        self._make_restore_child()
        self._journal(STATE_ROLLBACK_VERIFIED)

        result = self._recover()

        self.assertTrue(result.ok)
        self.assertEqual(result.state, STATE_ROLLED_BACK)
        self.assertTrue(result.recovered)
        self.assertTrue(result.source_restored)
        self.assertTrue(result.target_removed)
        self.assertTrue(result.data_kept)
        self.assertFalse(result.relinked)
        self.assertEqual(snapshot(self.source), expected_snapshot())
        self.assertFalse(self.final.exists())
        self.assertFalse(os.path.lexists(str(self.restore_parent)))

        journal = JournalStore(str(self.jobs_dir)).load(
            JOB_ID, JOURNAL_TYPE_ROLLBACK
        )
        self.assertEqual(journal.state, STATE_ROLLED_BACK)
        self.assertTrue(journal.recovered_at)

        records = HistoryStore(str(self.history_path)).find(
            JOB_ID, TYPE_ROLLBACK
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].result, RESULT_ROLLED_BACK)

        rules = LearnedRulesStore(str(self.learned_path)).load()
        key = item_id_for("demo", source_path=str(self.source))
        self.assertEqual(rules[key]["safety"], SAFETY_RED)

    def test_recovery_is_idempotent_once_terminal(self):
        self.fixture.make_final(owned=True)
        self._remove_source()
        self._make_restore_child()
        self._journal(STATE_ROLLBACK_VERIFIED)
        self._recover()

        result = self._recover()

        self.assertEqual(result.state, STATE_ROLLED_BACK)
        self.assertTrue(result.data_kept)
        self.assertEqual(snapshot(self.source), expected_snapshot())
        records = HistoryStore(str(self.history_path)).find(
            JOB_ID, TYPE_ROLLBACK
        )
        self.assertEqual(len(records), 1)

    # -- re-link when the junction was removed ---------------------------

    def test_missing_source_with_owned_target_is_relinked(self):
        self.fixture.make_final(owned=True)
        self._remove_source()
        self._journal(STATE_ROLLBACK_LINK_REMOVED)

        result = self._recover()

        self.assertFalse(result.ok)
        self.assertEqual(result.state, STATE_ROLLBACK_FAILED)
        self.assertTrue(result.relinked)
        self.assertFalse(result.link_removed)
        self.assertTrue(result.data_kept)
        self.assertTrue(self.final.exists())
        self.assertTrue(self.ops.is_junction(str(self.source)))
        self.assertEqual(
            self.ops.read_link_target(str(self.source)),
            str(self.final),
        )

    def test_junction_is_left_in_place_when_still_intact(self):
        self.fixture.make_final(owned=True)
        self.fixture.move_source_to_tombstone()
        self.ops.create_link(str(self.source), str(self.final))
        self._journal(STATE_ROLLBACK_PLANNED)

        result = self._recover()

        self.assertEqual(result.state, STATE_ROLLBACK_FAILED)
        self.assertFalse(result.link_removed)
        self.assertFalse(result.relinked)
        self.assertTrue(result.data_kept)
        self.assertTrue(self.ops.is_junction(str(self.source)))
        self.assertTrue(self.final.exists())

    # -- contradictory evidence requires a human -------------------------

    def test_plain_source_and_owned_target_in_planned_state_stops(self):
        self.fixture.make_final(owned=True)
        self._journal(STATE_ROLLBACK_PLANNED)

        result = self._recover()

        self.assertEqual(result.state, STATE_ROLLBACK_RECOVERY_REQUIRED)
        self.assertEqual(result.error_code, ERROR_RECOVERY)
        self.assertTrue(result.data_kept)
        self.assertTrue(self.final.exists())
        self.assertEqual(snapshot(self.source), expected_snapshot())

    def test_plain_source_and_owned_target_without_state_stops(self):
        self.fixture.make_final(owned=True)

        result = recover_rollback(
            JOB_ID,
            "demo",
            str(self.source),
            str(self.final),
            ops=self.ops,
            recorded_state=None,
        )

        self.assertEqual(result.state, STATE_ROLLBACK_RECOVERY_REQUIRED)
        self.assertTrue(result.data_kept)
        self.assertTrue(self.final.exists())

    def test_plain_source_and_unowned_target_stops(self):
        self.fixture.make_final(owned=False)
        self._journal(STATE_ROLLBACK_SOURCE_READY)

        result = self._recover()

        self.assertEqual(result.state, STATE_ROLLBACK_RECOVERY_REQUIRED)
        self.assertTrue(result.data_kept)
        self.assertTrue(self.final.exists())

    def test_unverified_restore_copy_is_not_promoted(self):
        self.fixture.make_final(owned=True)
        self._remove_source()
        self._make_restore_child()
        self._journal(STATE_ROLLBACK_COPYING)

        result = self._recover()

        self.assertEqual(result.state, STATE_ROLLBACK_FAILED)
        self.assertTrue(result.relinked)
        self.assertTrue(self.final.exists())
        self.assertTrue(self.ops.is_junction(str(self.source)))
        # The partial restore slot is job-scoped and removable; the target is
        # the only complete copy and stays untouched.
        self.assertFalse(os.path.lexists(str(self.restore_parent)))

    def test_orphan_restore_copy_without_target_stops(self):
        self.fixture.make_final(owned=True)
        self._remove_source()
        self._make_restore_child()
        shutil.rmtree(str(self.final))
        self._journal(STATE_ROLLBACK_COPIED)

        result = self._recover()

        self.assertEqual(result.state, STATE_ROLLBACK_RECOVERY_REQUIRED)
        self.assertTrue(result.data_kept)
        self.assertTrue(self.restore_child.is_dir())
        self.assertFalse(self.source.exists())

    def test_missing_source_and_unowned_target_stops(self):
        self.fixture.make_final(owned=False)
        self._remove_source()
        self._journal(STATE_ROLLBACK_LINK_REMOVED)

        result = self._recover()

        self.assertEqual(result.state, STATE_ROLLBACK_RECOVERY_REQUIRED)
        self.assertTrue(result.data_kept)
        self.assertTrue(self.final.exists())
        self.assertFalse(self.ops.is_junction(str(self.source)))

    def test_non_junction_link_is_never_removed(self):
        self.fixture.make_final(owned=True)
        self._remove_source()
        self.ops.links[self.ops._key(str(self.source))] = str(self.final)
        self.ops.is_junction = lambda path: False
        self._journal(STATE_ROLLBACK_LINK_REMOVED)

        result = self._recover()

        self.assertEqual(result.state, STATE_ROLLBACK_RECOVERY_REQUIRED)
        self.assertTrue(self.ops.exists(str(self.source)))
        self.assertTrue(self.final.exists())

    def test_source_that_is_a_file_stops(self):
        self.fixture.make_final(owned=True)
        self._remove_source()
        self.source.write_text("not a directory", encoding="utf-8")
        self._journal(STATE_ROLLBACK_LINK_REMOVED)

        result = self._recover()

        self.assertEqual(result.state, STATE_ROLLBACK_RECOVERY_REQUIRED)
        self.assertTrue(self.source.is_file())
        self.assertTrue(self.final.exists())

    def test_restore_slot_that_is_a_file_stops(self):
        self.fixture.make_final(owned=True)
        self._remove_source()
        self.restore_parent.write_text("not a directory", encoding="utf-8")
        self._journal(STATE_ROLLBACK_VERIFIED)

        result = self._recover()

        self.assertEqual(result.state, STATE_ROLLBACK_RECOVERY_REQUIRED)
        self.assertTrue(self.restore_parent.is_file())
        self.assertTrue(self.final.exists())

    def test_missing_rollback_journal_stops(self):
        self.fixture.make_final(owned=True)
        self.fixture.move_source_to_tombstone()
        self.ops.create_link(str(self.source), str(self.final))

        result = self._recover()

        self.assertEqual(result.state, STATE_ROLLBACK_RECOVERY_REQUIRED)
        self.assertEqual(result.error_code, ERROR_RECOVERY)
        self.assertTrue(result.data_kept)
        self.assertTrue(self.ops.is_junction(str(self.source)))

    # -- restore-slot validation -----------------------------------------

    def test_restore_dir_outside_the_source_parent_stops(self):
        self.fixture.make_final(owned=True)
        self._remove_source()
        elsewhere = self.root / "elsewhere" / (".dw-restore-" + JOB8)
        shutil.copytree(str(self.final), str(elsewhere / self.source.name))
        self._journal(STATE_ROLLBACK_VERIFIED, restore_dir=str(elsewhere))

        result = self._recover()

        self.assertEqual(result.state, STATE_ROLLBACK_RECOVERY_REQUIRED)
        self.assertTrue(result.data_kept)
        self.assertTrue(elsewhere.is_dir())
        self.assertTrue(self.final.exists())

    def test_restore_dir_for_another_job_stops(self):
        self.fixture.make_final(owned=True)
        self._remove_source()
        foreign = self.source_parent / ".dw-restore-deadbeef"
        shutil.copytree(str(self.final), str(foreign / self.source.name))
        self._journal(STATE_ROLLBACK_VERIFIED, restore_dir=str(foreign))

        result = self._recover()

        self.assertEqual(result.state, STATE_ROLLBACK_RECOVERY_REQUIRED)
        self.assertTrue(foreign.is_dir())
        self.assertFalse(self.source.exists())

    # -- terminal journal states -----------------------------------------

    def test_terminal_journal_state_is_not_re_executed(self):
        self.fixture.make_final(owned=True)
        self.fixture.move_source_to_tombstone()
        self.ops.create_link(str(self.source), str(self.final))
        self._journal(JOURNAL_STATE_ROLLED_BACK)

        result = self._recover()

        self.assertTrue(result.ok)
        self.assertEqual(result.state, JOURNAL_STATE_ROLLED_BACK)
        self.assertTrue(result.data_kept)
        self.assertTrue(self.ops.is_junction(str(self.source)))
        self.assertTrue(self.final.exists())
        self.assertFalse(result.history_recorded)

    def test_config_restored_state_still_finishes_cleanup(self):
        self.fixture.make_final(owned=True)
        self._journal(STATE_ROLLBACK_CONFIG_RESTORED)

        result = self._recover()

        self.assertEqual(result.state, STATE_ROLLED_BACK)
        self.assertTrue(result.source_restored)
        self.assertTrue(result.target_removed)
        self.assertEqual(snapshot(self.source), expected_snapshot())


if __name__ == "__main__":
    unittest.main()
