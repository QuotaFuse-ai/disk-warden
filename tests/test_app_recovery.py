# -*- coding: utf-8 -*-
"""Startup recovery decisions over the journal (T12 batch 3).

``recovery_list`` is the only place that decides whether a journal entry
needs the user's attention, so these tests pin the mapping from journal state
to offered actions.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from src.app.errors import BridgeError
from src.app.service import STATE_LABELS
from src.core.migrate import write_owner_marker
from src.platform import LinkError, create_junction, remove_junction
from src.core.store.journal import (
    STATE_ALREADY_ROLLED_BACK,
    STATE_DONE,
    STATE_FAILED,
    STATE_LINKED,
    STATE_LINKED_CLEANUP_PENDING,
    STATE_RECOVERY_REQUIRED,
    STATE_ROLLBACK_COPYING,
    STATE_ROLLBACK_FAILED,
    STATE_ROLLBACK_RECOVERY_REQUIRED,
    STATE_ROLLED_BACK,
    TYPE_MIGRATION,
    TYPE_ROLLBACK,
)
from tests.app_fakes import SOURCE_CACHE, make_service

MIGRATION_ID = "aaaabbbbccccdddd"
ROLLBACK_ID = "1111222233334444"


def add_journal(service, job_id, entry_type, state, bytes_=123):
    return service.journals.begin(
        job_id,
        entry_type=entry_type,
        tool_id="cursor",
        source=SOURCE_CACHE,
        state=state,
        intent={"bytes": bytes_},
    )


class RecoveryListTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service(self)

    def _items(self):
        return {item["job_id"]: item for item in self.service.recovery_list()["items"]}

    def test_unfinished_migration_offers_rollback_and_recover(self):
        add_journal(self.service, MIGRATION_ID, TYPE_MIGRATION, STATE_LINKED)
        result = self.service.recovery_list()
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["warnings"], [])
        item = result["items"][0]
        self.assertEqual(item["actions"], ["rollback", "recover"])
        self.assertFalse(item["needs_human"])
        self.assertEqual(item["state"], STATE_LINKED)
        self.assertEqual(item["state_label"], STATE_LABELS[STATE_LINKED])
        self.assertEqual(item["bytes"], 123)
        self.assertEqual(item["type"], TYPE_MIGRATION)
        self.assertTrue(item["final_path"])

    def test_recovery_required_needs_a_human(self):
        add_journal(
            self.service, MIGRATION_ID, TYPE_MIGRATION, STATE_RECOVERY_REQUIRED
        )
        item = self._items()[MIGRATION_ID]
        self.assertEqual(item["actions"], ["recover", "rollback"])
        self.assertTrue(item["needs_human"])

    def test_cleanup_pending_only_offers_recover(self):
        add_journal(
            self.service,
            MIGRATION_ID,
            TYPE_MIGRATION,
            STATE_LINKED_CLEANUP_PENDING,
        )
        item = self._items()[MIGRATION_ID]
        self.assertEqual(item["actions"], ["recover"])
        self.assertFalse(item["needs_human"])
        self.assertIn("清理", item["label"])

    def test_finished_migration_is_not_reported(self):
        add_journal(self.service, MIGRATION_ID, TYPE_MIGRATION, STATE_DONE)
        add_journal(self.service, "5555666677778888", TYPE_MIGRATION, STATE_FAILED)
        self.assertEqual(self.service.recovery_list()["items"], [])

    def test_unfinished_rollback_offers_recover(self):
        add_journal(
            self.service, ROLLBACK_ID, TYPE_ROLLBACK, STATE_ROLLBACK_COPYING
        )
        item = self._items()[ROLLBACK_ID]
        self.assertEqual(item["type"], TYPE_ROLLBACK)
        self.assertEqual(item["actions"], ["recover"])
        self.assertFalse(item["needs_human"])
        self.assertEqual(item["label"], "上次回滚未完成")

    def test_rollback_recovery_required_needs_a_human(self):
        add_journal(
            self.service,
            ROLLBACK_ID,
            TYPE_ROLLBACK,
            STATE_ROLLBACK_RECOVERY_REQUIRED,
        )
        item = self._items()[ROLLBACK_ID]
        self.assertEqual(item["actions"], ["recover"])
        self.assertTrue(item["needs_human"])

    def test_finished_rollback_is_not_reported(self):
        add_journal(
            self.service, ROLLBACK_ID, TYPE_ROLLBACK, STATE_ALREADY_ROLLED_BACK
        )
        self.assertEqual(self.service.recovery_list()["items"], [])

    def test_items_are_sorted_newest_first(self):
        add_journal(self.service, MIGRATION_ID, TYPE_MIGRATION, STATE_LINKED)
        add_journal(self.service, "99990000aaaabbbb", TYPE_MIGRATION, STATE_LINKED)
        items = self.service.recovery_list()["items"]
        self.assertEqual(len(items), 2)
        stamps = [item["updated_at"] for item in items]
        self.assertEqual(stamps, sorted(stamps, reverse=True))


class RecoveryRunValidationTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service(self)

    def test_job_id_is_required(self):
        for value in (None, "", 7):
            with self.assertRaises(BridgeError) as ctx:
                self.service.recovery_run(value)
            self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")

    def test_action_must_be_known(self):
        with self.assertRaises(BridgeError) as ctx:
            self.service.recovery_run(MIGRATION_ID, "delete-everything")
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")
        self.assertIn("recover", ctx.exception.detail["reason"])

    def test_unknown_job_is_not_found(self):
        with self.assertRaises(BridgeError) as ctx:
            self.service.recovery_run(MIGRATION_ID, "recover")
        self.assertEqual(ctx.exception.code, "NOT_FOUND")
        self.assertEqual(ctx.exception.detail["job_id"], MIGRATION_ID)

    def test_a_rollback_entry_cannot_be_rolled_back_again(self):
        add_journal(
            self.service, ROLLBACK_ID, TYPE_ROLLBACK, STATE_ROLLBACK_COPYING
        )
        with self.assertRaises(BridgeError) as ctx:
            self.service.recovery_run(ROLLBACK_ID, "rollback")
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")
        self.assertIn("migration", ctx.exception.detail["reason"])
        self.assertEqual(self.service.runner.active_jobs(), [])


class CrashWindowTest(unittest.TestCase):
    """The S6/S7 crash must reach a terminal state from either button.

    The source was renamed to the job's tombstone, the junction was never
    created and the tool recreated its directory.  Both "recover" and
    "rollback" used to leave the entry pending forever, so the banner came
    back on every refresh.
    """

    JOB_ID = "0123456789abcdef"
    JOB8 = "01234567"

    def setUp(self):
        self.service = make_service(self)
        temp = tempfile.TemporaryDirectory(prefix="dw-crash-window-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.source = root / "Cursor" / "Cache"
        (self.source / "sub").mkdir(parents=True)
        (self.source / "a.bin").write_bytes(b"a" * 100)
        (self.source / "sub" / "b.bin").write_bytes(b"b" * 200)
        self.final = root / "data" / "cursor" / "Cache"
        shutil.copytree(str(self.source), str(self.final))
        write_owner_marker(
            str(self.final),
            self.JOB_ID,
            "cursor",
            str(self.source),
            str(self.final),
            file_count=2,
            total_bytes=300,
        )
        self.tombstone = root / "Cursor" / (".dwtombstone-" + self.JOB8)
        shutil.copytree(str(self.source), str(self.tombstone))
        self.service.journals.begin(
            self.JOB_ID,
            tool_id="cursor",
            source=str(self.source),
            final=str(self.final),
            state=STATE_RECOVERY_REQUIRED,
            intent={"bytes": 300},
        )

    def run_recovery(self, action):
        runner_id = self.service.recovery_run(self.JOB_ID, action)["job_id"]
        self.service.runner.wait(runner_id, 20)

    def assert_settled(self):
        entry = self.service.journals.load(self.JOB_ID, TYPE_MIGRATION)
        self.assertEqual(entry.state, STATE_ROLLED_BACK)
        self.assertEqual(self.service.recovery_list()["items"], [])
        self.assertTrue(self.source.joinpath("a.bin").is_file())
        self.assertTrue(self.source.joinpath("sub", "b.bin").is_file())
        self.assertFalse(self.tombstone.exists())
        self.assertFalse(self.final.exists())

    def test_recover_reaches_a_terminal_state(self):
        self.run_recovery("recover")
        self.assert_settled()

    def test_rolled_back_recovery_reports_success(self):
        # The UI treats summary.ok false as "恢复失败"; a recovery that
        # rolled the interrupted migration back is a success, not a failure.
        runner_id = self.service.recovery_run(self.JOB_ID, "recover")["job_id"]
        job = self.service.runner.wait(runner_id, 20)
        self.assertEqual(job.get("state"), "done")
        self.assertEqual(job["summary"]["state"], STATE_ROLLED_BACK)
        self.assertTrue(job["summary"]["ok"], job["summary"])

    def test_rollback_on_an_unlinked_source_uses_recovery(self):
        self.run_recovery("rollback")
        self.assert_settled()

    def test_running_recovery_twice_is_harmless(self):
        self.run_recovery("recover")
        self.run_recovery("recover")
        self.assert_settled()

    def test_source_is_junction_detection(self):
        plain = str(self.source)
        self.assertFalse(self.service._source_is_junction(plain))
        self.assertFalse(self.service._source_is_junction(""))
        link = str(self.source.parent / "CacheLink")
        # Use the platform adapter instead of ``cmd /c mklink /J`` so the test
        # exercises the real link type on every host (junction on Windows,
        # symlink on macOS/POSIX).
        try:
            create_junction(link, str(self.final))
        except (LinkError, OSError) as exc:
            self.skipTest("this filesystem cannot create a directory link: %s" % exc)
        self.addCleanup(remove_junction, link)
        self.assertTrue(self.service._source_is_junction(link))


class BannerConvergenceTest(unittest.TestCase):
    """The banner must be clearable (T27).

    A failure the engine already considers final used to stay listed, so
    "recover" reported success and the banner came back on every refresh.
    Entries that genuinely need a human now carry a hide action.
    """

    def setUp(self):
        self.service = make_service(self)

    def _items(self):
        return {item["job_id"]: item for item in self.service.recovery_list()["items"]}

    def test_failed_rollback_is_not_listed(self):
        # The rollback engine treats ROLLBACK_FAILED as final; listing it
        # promised a progress path that did not exist.
        add_journal(
            self.service, ROLLBACK_ID, TYPE_ROLLBACK, STATE_ROLLBACK_FAILED
        )
        self.assertEqual(self.service.recovery_list()["items"], [])

    def test_one_row_per_job_when_both_records_exist(self):
        add_journal(self.service, MIGRATION_ID, TYPE_MIGRATION, STATE_RECOVERY_REQUIRED)
        add_journal(
            self.service, MIGRATION_ID, TYPE_ROLLBACK, STATE_ROLLBACK_RECOVERY_REQUIRED
        )
        items = self.service.recovery_list()["items"]
        self.assertEqual([item["job_id"] for item in items], [MIGRATION_ID])
        self.assertEqual(items[0]["type"], TYPE_MIGRATION)

    def test_attention_states_are_ignorable(self):
        add_journal(self.service, MIGRATION_ID, TYPE_MIGRATION, STATE_RECOVERY_REQUIRED)
        item = self._items()[MIGRATION_ID]
        self.assertTrue(item["ignorable"])

    def test_actionable_states_are_not_ignorable(self):
        add_journal(self.service, MIGRATION_ID, TYPE_MIGRATION, STATE_LINKED)
        self.assertFalse(self._items()[MIGRATION_ID]["ignorable"])

    def test_ignored_entry_disappears(self):
        add_journal(self.service, MIGRATION_ID, TYPE_MIGRATION, STATE_RECOVERY_REQUIRED)
        self.service.recovery_ignore(MIGRATION_ID)
        self.assertEqual(self.service.recovery_list()["items"], [])
        # The journal itself is untouched: the record is still loadable.
        entry = self.service.journals.load(MIGRATION_ID, TYPE_MIGRATION)
        self.assertEqual(entry.state, STATE_RECOVERY_REQUIRED)

    def test_ignore_survives_a_reload(self):
        add_journal(self.service, MIGRATION_ID, TYPE_MIGRATION, STATE_RECOVERY_REQUIRED)
        self.service.recovery_ignore(MIGRATION_ID)
        hidden = self.service.journals.ignored()
        self.assertEqual(
            hidden["%s:%s" % (TYPE_MIGRATION, MIGRATION_ID)],
            STATE_RECOVERY_REQUIRED,
        )

    def test_a_new_state_becomes_visible_again(self):
        add_journal(self.service, MIGRATION_ID, TYPE_MIGRATION, STATE_LINKED_CLEANUP_PENDING)
        self.service.recovery_ignore(MIGRATION_ID)
        self.assertEqual(self.service.recovery_list()["items"], [])
        self.service.journals.set_result(
            MIGRATION_ID, STATE_RECOVERY_REQUIRED, entry_type=TYPE_MIGRATION
        )
        self.assertIn(MIGRATION_ID, self._items())

    def test_ignore_rejects_an_entry_with_an_automatic_path(self):
        add_journal(self.service, MIGRATION_ID, TYPE_MIGRATION, STATE_LINKED)
        with self.assertRaises(BridgeError) as ctx:
            self.service.recovery_ignore(MIGRATION_ID)
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")
        self.assertIn(MIGRATION_ID, self._items())

    def test_ignore_requires_a_known_job(self):
        with self.assertRaises(BridgeError) as ctx:
            self.service.recovery_ignore(MIGRATION_ID)
        self.assertEqual(ctx.exception.code, "NOT_FOUND")

    def test_corrupt_ignore_file_hides_nothing(self):
        add_journal(self.service, MIGRATION_ID, TYPE_MIGRATION, STATE_RECOVERY_REQUIRED)
        path = os.path.join(self.service.journals.jobs_dir, ".ignored.json")
        with open(path, "w") as handle:
            handle.write("{not json")
        self.assertIn(MIGRATION_ID, self._items())

    def test_amount_is_reported_once_per_job(self):
        add_journal(self.service, MIGRATION_ID, TYPE_MIGRATION, STATE_RECOVERY_REQUIRED)
        add_journal(self.service, ROLLBACK_ID, TYPE_ROLLBACK, STATE_ROLLBACK_COPYING)
        self.assertEqual(self.service.recovery_list()["count"], 2)


if __name__ == "__main__":
    unittest.main()
