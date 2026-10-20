# -*- coding: utf-8 -*-
"""Startup recovery decisions over the journal (T12 batch 3).

``recovery_list`` is the only place that decides whether a journal entry
needs the user's attention, so these tests pin the mapping from journal state
to offered actions.
"""

import unittest

from src.app.errors import BridgeError
from src.app.service import STATE_LABELS
from src.core.store.journal import (
    STATE_ALREADY_ROLLED_BACK,
    STATE_DONE,
    STATE_FAILED,
    STATE_LINKED,
    STATE_LINKED_CLEANUP_PENDING,
    STATE_RECOVERY_REQUIRED,
    STATE_ROLLBACK_COPYING,
    STATE_ROLLBACK_RECOVERY_REQUIRED,
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


if __name__ == "__main__":
    unittest.main()
