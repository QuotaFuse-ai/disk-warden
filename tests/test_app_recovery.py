# -*- coding: utf-8 -*-
"""Startup recovery decisions over the journal (T12 batch 3).

``recovery_list`` is the only place that decides whether a journal entry
needs the user's attention, so these tests pin the mapping from journal state
to offered actions.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from src.app.errors import BridgeError
from src.app.service import STATE_LABELS
from src.core.migrate import write_owner_marker
from src.core.store.journal import (
    STATE_ALREADY_ROLLED_BACK,
    STATE_DONE,
    STATE_FAILED,
    STATE_LINKED,
    STATE_LINKED_CLEANUP_PENDING,
    STATE_RECOVERY_REQUIRED,
    STATE_ROLLBACK_COPYING,
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
        created = subprocess.call(
            ["cmd", "/c", "mklink", "/J", link, str(self.final)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if created != 0:
            self.skipTest("mklink /J is unavailable in this environment")
        self.addCleanup(self._remove_link, link)
        self.assertTrue(self.service._source_is_junction(link))

    @staticmethod
    def _remove_link(path):
        try:
            os.rmdir(path)
        except OSError:
            pass


if __name__ == "__main__":
    unittest.main()
