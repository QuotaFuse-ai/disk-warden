# -*- coding: utf-8 -*-
"""History store: append-only records, statistics, quarantine and archive."""

import datetime
import os
import tempfile
import unittest
from unittest import mock

from src.core.store import atomic
from src.core.store.history import (
    HistoryRecord,
    HistoryStore,
    RESULT_FAILURE,
    RESULT_ROLLED_BACK,
    RESULT_SUCCESS,
    TYPE_MIGRATION,
    TYPE_ROLLBACK,
    normalize_record,
)


def codes(store):
    return [warning["code"] for warning in store.warnings]


class HistoryStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.path = os.path.join(self.root, "config", "history.json")
        self.store = HistoryStore(self.path)

    def _record(self, job_id="job-1", size=100, result=RESULT_SUCCESS):
        return HistoryRecord(
            time="2026-01-01T00:00:00Z",
            type=TYPE_MIGRATION,
            job_id=job_id,
            tool="demo",
            item="Cache",
            source="C:/Cache",
            target="D:/DiskWarden/data/demo/Cache",
            bytes=size,
            result=result,
        )

    def test_append_and_load_round_trip(self):
        self.store.append(self._record())
        self.store.append(
            HistoryRecord(
                time="2026-01-01T00:00:01Z",
                type=TYPE_ROLLBACK,
                job_id="job-1",
                tool="demo",
                item="Cache",
                bytes=100,
                result=RESULT_ROLLED_BACK,
            )
        )
        loaded = HistoryStore(self.path).load()
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0].type, TYPE_MIGRATION)
        self.assertEqual(loaded[1].result, RESULT_ROLLED_BACK)
        self.assertTrue(self.store.complete)

    def test_missing_file_is_reported_incomplete(self):
        self.assertEqual(self.store.load(), [])
        self.assertFalse(self.store.complete)
        self.assertIn("history_missing", codes(self.store))

    def test_record_migration_and_rollback_set_type_and_result(self):
        migration = self.store.record_migration(
            "demo", "Cache", "C:/Cache", "D:/Cache", 10
        )
        rollback = self.store.record_rollback(
            "demo", "Cache", "C:/Cache", "D:/Cache", 10
        )
        self.assertEqual(migration.type, TYPE_MIGRATION)
        self.assertEqual(migration.result, RESULT_SUCCESS)
        self.assertEqual(rollback.type, TYPE_ROLLBACK)
        self.assertEqual(rollback.result, RESULT_ROLLED_BACK)

    def test_find_and_was_rolled_back(self):
        self.store.append(self._record(job_id="job-1"))
        self.store.append(
            HistoryRecord(
                time="2026-01-01T00:00:01Z",
                type=TYPE_ROLLBACK,
                job_id="job-1",
                result=RESULT_ROLLED_BACK,
            )
        )
        self.assertEqual(len(self.store.find("job-1")), 2)
        self.assertEqual(len(self.store.find("job-1", TYPE_MIGRATION)), 1)
        self.assertTrue(self.store.was_rolled_back("job-1"))
        self.assertFalse(self.store.was_rolled_back("job-2"))

    def test_released_stats_excludes_rolled_back_jobs(self):
        self.store.append(self._record(job_id="job-1", size=100))
        self.store.append(self._record(job_id="job-2", size=50))
        self.store.append(
            HistoryRecord(
                time="2026-01-01T00:00:01Z",
                type=TYPE_ROLLBACK,
                job_id="job-1",
                result=RESULT_ROLLED_BACK,
            )
        )
        stats = self.store.released_stats()
        self.assertEqual(stats.total_bytes, 50)
        self.assertEqual(stats.migrations, 2)
        self.assertEqual(stats.rolled_back, 1)
        self.assertTrue(stats.complete)

    def test_released_stats_ignores_failed_migrations(self):
        self.store.append(
            self._record(job_id="job-1", size=100, result=RESULT_FAILURE)
        )
        stats = self.store.released_stats()
        self.assertEqual(stats.total_bytes, 0)
        self.assertEqual(stats.migrations, 0)

    def test_invalid_records_are_skipped_and_reported(self):
        payload = {
            "schema_version": 1,
            "records": [
                self._record().to_dict(),
                {"type": "unknown", "result": RESULT_SUCCESS, "bytes": 1},
                {"type": TYPE_MIGRATION, "result": RESULT_SUCCESS, "bytes": -1},
                "not-a-record",
            ],
        }
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "wb") as handle:
            handle.write(atomic.dump_json_bytes(payload))

        loaded = self.store.load()
        self.assertEqual(len(loaded), 1)
        self.assertFalse(self.store.complete)
        self.assertIn("history_record_invalid", codes(self.store))

    def test_bare_list_payload_is_accepted(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "wb") as handle:
            handle.write(atomic.dump_json_bytes([self._record().to_dict()]))
        self.assertEqual(len(self.store.load()), 1)

    def test_corrupt_file_is_quarantined(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "wb") as handle:
            handle.write(b"{not json")
        self.assertEqual(self.store.load(), [])
        self.assertIn("history_corrupt", codes(self.store))
        self.assertTrue(
            any(".corrupt-" in name for name in os.listdir(os.path.dirname(self.path)))
        )

    def test_unexpected_shape_is_quarantined(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "wb") as handle:
            handle.write(atomic.dump_json_bytes({"records": "nope"}))
        self.assertEqual(self.store.load(), [])
        self.assertIn("history_corrupt", codes(self.store))

    def test_cap_archives_oldest_records(self):
        store = HistoryStore(self.path, max_records=2)
        for index in range(3):
            store.append(self._record(job_id="job-%d" % index))

        kept = HistoryStore(self.path).load()
        self.assertEqual([record.job_id for record in kept], ["job-1", "job-2"])

        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")
        archive = os.path.join(self.root, "config", "history-%s.json" % stamp)
        self.assertTrue(os.path.isfile(archive))
        archived = HistoryStore(archive).load()
        self.assertEqual([record.job_id for record in archived], ["job-0"])

    def test_archive_failure_keeps_every_record(self):
        store = HistoryStore(self.path, max_records=1)
        with mock.patch.object(
            HistoryStore, "_archive", side_effect=atomic.StoreError("disk full")
        ):
            store.append(self._record(job_id="job-0"))
            store.append(self._record(job_id="job-1"))

        self.assertEqual(len(store.records), 2)
        self.assertIn("history_archive_failed", codes(store))
        self.assertEqual(len(HistoryStore(self.path).load()), 2)

    def test_save_rejects_invalid_record(self):
        with self.assertRaises(atomic.StoreError):
            self.store.save([{"type": "bogus"}])

    def test_save_accepts_dict_records(self):
        saved = self.store.save([self._record().to_dict()])
        self.assertIsInstance(saved[0], HistoryRecord)
        self.assertEqual(len(HistoryStore(self.path).load()), 1)

    def test_normalize_record_fills_missing_time(self):
        record = normalize_record(
            {"type": TYPE_MIGRATION, "result": RESULT_SUCCESS, "bytes": 1},
            now=datetime.datetime(2026, 2, 3, tzinfo=datetime.timezone.utc),
        )
        self.assertEqual(record.time, "2026-02-03T00:00:00Z")

    def test_empty_path_is_rejected(self):
        with self.assertRaises(atomic.StoreError):
            HistoryStore("")


if __name__ == "__main__":
    unittest.main()
