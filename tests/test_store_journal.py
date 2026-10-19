# -*- coding: utf-8 -*-
"""Journal store: intent/result records, listing and quarantine."""

import datetime
import os
import tempfile
import unittest

from src.core.store import atomic
from src.core.store.journal import (
    JournalEntry,
    JournalStore,
    STATE_DONE,
    STATE_PLANNED,
    STATE_ROLLBACK_PLANNED,
    TYPE_MIGRATION,
    TYPE_ROLLBACK,
    entry_from_dict,
    validate_job_id,
)

JOB_A = "aaaaaaaa11111111"
JOB_B = "bbbbbbbb22222222"


def moment(second):
    return datetime.datetime(2026, 1, 1, 0, 0, second, tzinfo=datetime.timezone.utc)


def codes(store):
    return [warning["code"] for warning in store.warnings]


class JournalStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.jobs_dir = os.path.join(self.tmp.name, "config", "migration-jobs")
        self.store = JournalStore(self.jobs_dir)

    def test_path_for_uses_exact_names(self):
        self.assertEqual(
            self.store.path_for(JOB_A),
            os.path.join(self.jobs_dir, JOB_A + ".json"),
        )
        self.assertEqual(
            self.store.path_for(JOB_A, TYPE_ROLLBACK),
            os.path.join(self.jobs_dir, JOB_A + "-rollback.json"),
        )

    def test_path_for_rejects_invalid_input(self):
        with self.assertRaises(atomic.StoreError):
            self.store.path_for("../../escape")
        with self.assertRaises(atomic.StoreError):
            self.store.path_for(JOB_A, "other")

    def test_validate_job_id_lowercases(self):
        self.assertEqual(validate_job_id("ABCDEF" + "0" * 26), "abcdef" + "0" * 26)

    def test_begin_writes_intent_record(self):
        entry = self.store.begin(
            JOB_A,
            tool_id="demo",
            source="C:/Cache",
            final="D:/Cache",
            backup_dir="D:/backup",
            intent={"step": "copy"},
            now=moment(0),
        )
        self.assertEqual(entry.created_at, entry.updated_at)
        loaded = self.store.load(JOB_A)
        self.assertEqual(loaded.tool_id, "demo")
        self.assertEqual(loaded.state, STATE_PLANNED)
        self.assertEqual(loaded.intent, {"step": "copy"})
        self.assertTrue(os.path.isfile(self.store.path_for(JOB_A)))

    def test_set_intent_and_result(self):
        self.store.begin(JOB_A, now=moment(0))
        self.store.set_intent(
            JOB_A, "copy", payload={"about": "copy"}, state="COPYING", now=moment(1)
        )
        entry = self.store.load(JOB_A)
        self.assertEqual(entry.step, "copy")
        self.assertEqual(entry.state, "COPYING")
        self.assertEqual(entry.intent, {"about": "copy"})

        self.store.set_result(
            JOB_A, STATE_DONE, payload={"ok": True}, step="record", now=moment(2)
        )
        entry = self.store.load(JOB_A)
        self.assertEqual(entry.state, STATE_DONE)
        self.assertEqual(entry.step, "record")
        self.assertEqual(entry.result, {"ok": True})
        self.assertTrue(entry.terminal)

    def test_set_intent_keeps_existing_values_when_empty(self):
        self.store.begin(JOB_A, step="first", intent={"a": 1}, now=moment(0))
        self.store.set_intent(JOB_A, "", payload=None, now=moment(1))
        entry = self.store.load(JOB_A)
        self.assertEqual(entry.step, "first")
        self.assertEqual(entry.intent, {"a": 1})

    def test_mark_recovered_sets_timestamp(self):
        self.store.begin(JOB_A, now=moment(0))
        entry = self.store.mark_recovered(
            JOB_A, STATE_DONE, payload={"recovered": True}, now=moment(5)
        )
        self.assertEqual(entry.recovered_at, "2026-01-01T00:00:05Z")
        self.assertEqual(entry.state, STATE_DONE)

    def test_update_rejects_immutable_and_unknown_fields(self):
        self.store.begin(JOB_A)
        for key in ("type", "created_at", "nope"):
            with self.assertRaises(atomic.StoreError):
                self.store.update(JOB_A, **{key: "x"})

    def test_update_requires_mapping_payloads(self):
        self.store.begin(JOB_A)
        with self.assertRaises(atomic.StoreError):
            self.store.update(JOB_A, intent=["not", "a", "dict"])

    def test_update_persists_changes(self):
        self.store.begin(JOB_A)
        entry = self.store.update(JOB_A, state="COPYING", step="copy")
        self.assertEqual(entry.state, "COPYING")
        self.assertEqual(self.store.load(JOB_A).step, "copy")

    def test_update_missing_entry_raises(self):
        with self.assertRaises(atomic.StoreError):
            self.store.update(JOB_A, state=STATE_DONE)

    def test_load_missing_returns_none(self):
        self.assertIsNone(self.store.load(JOB_A))

    def test_corrupt_file_is_quarantined(self):
        os.makedirs(self.jobs_dir, exist_ok=True)
        with open(self.store.path_for(JOB_A), "wb") as handle:
            handle.write(b"{broken")
        self.assertIsNone(self.store.load(JOB_A))
        self.assertIn("journal_corrupt", codes(self.store))
        self.assertTrue(
            any(".corrupt-" in name for name in os.listdir(self.jobs_dir))
        )

    def test_invalid_record_is_reported(self):
        os.makedirs(self.jobs_dir, exist_ok=True)
        with open(self.store.path_for(JOB_A), "wb") as handle:
            handle.write(atomic.dump_json_bytes({"job_id": JOB_A}))
        self.assertIsNone(self.store.load(JOB_A))
        self.assertIn("journal_invalid", codes(self.store))

    def test_record_with_other_job_id_is_reported(self):
        os.makedirs(self.jobs_dir, exist_ok=True)
        entry = JournalEntry(job_id=JOB_B, state=STATE_PLANNED)
        with open(self.store.path_for(JOB_A), "wb") as handle:
            handle.write(atomic.dump_json_bytes(entry.to_dict()))
        self.assertIsNone(self.store.load(JOB_A))
        self.assertIn("journal_invalid", codes(self.store))

    def test_type_mismatch_is_reported(self):
        os.makedirs(self.jobs_dir, exist_ok=True)
        entry = JournalEntry(job_id=JOB_A, type=TYPE_MIGRATION)
        with open(self.store.path_for(JOB_A, TYPE_ROLLBACK), "wb") as handle:
            handle.write(atomic.dump_json_bytes(entry.to_dict()))
        self.assertIsNone(self.store.load(JOB_A, TYPE_ROLLBACK))
        self.assertIn("journal_type_mismatch", codes(self.store))

    def test_list_pending_skips_terminal_temp_corrupt_and_invalid_names(self):
        self.store.begin(JOB_A, now=moment(0))
        self.store.begin(JOB_B, now=moment(1))
        self.store.set_result(JOB_B, STATE_DONE, now=moment(2))
        rollback = self.store.begin(
            JOB_A, entry_type=TYPE_ROLLBACK, state=STATE_ROLLBACK_PLANNED,
            now=moment(3),
        )
        self.assertEqual(rollback.state, STATE_ROLLBACK_PLANNED)

        for name in (
            JOB_A + ".json.tmp-1",
            JOB_A + "-rollback.json.corrupt-1",
            "not-a-job.json",
            "notes.txt",
        ):
            with open(os.path.join(self.jobs_dir, name), "wb") as handle:
                handle.write(b"{}")

        pending = self.store.list_pending()
        self.assertEqual(len(pending), 2)
        self.assertEqual(pending[0].type, TYPE_ROLLBACK)
        self.assertEqual(pending[0].updated_at, "2026-01-01T00:00:03Z")
        self.assertEqual(pending[1].job_id, JOB_A)
        self.assertEqual(pending[1].type, TYPE_MIGRATION)

    def test_list_pending_missing_directory_is_empty(self):
        self.assertEqual(self.store.list_pending(), [])

    def test_list_all_includes_terminal_records(self):
        self.store.begin(JOB_A, now=moment(0))
        self.store.begin(JOB_B, now=moment(1))
        self.store.set_result(JOB_B, STATE_DONE, now=moment(2))
        entries = self.store.list_all()
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].job_id, JOB_B)
        self.assertTrue(entries[0].terminal)

    def test_remove(self):
        self.store.begin(JOB_A)
        self.assertTrue(self.store.remove(JOB_A))
        self.assertFalse(self.store.remove(JOB_A))
        self.assertFalse(os.path.exists(self.store.path_for(JOB_A)))

    def test_empty_directory_is_rejected(self):
        with self.assertRaises(atomic.StoreError):
            JournalStore("")

    def test_entry_from_dict_rejects_bad_payloads(self):
        self.assertIsNone(entry_from_dict(None))
        self.assertIsNone(entry_from_dict({"job_id": "XYZ", "state": "PLANNED"}))
        self.assertIsNone(
            entry_from_dict({"job_id": JOB_A, "state": "", "type": "migration"})
        )
        self.assertIsNone(
            entry_from_dict(
                {"job_id": JOB_A, "state": "PLANNED", "type": "bogus"}
            )
        )


if __name__ == "__main__":
    unittest.main()
