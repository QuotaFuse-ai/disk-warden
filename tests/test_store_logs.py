# -*- coding: utf-8 -*-
"""JSONL diagnostic log: whitelist, masking, rotation and failure tolerance."""

import datetime
import json
import os
import tempfile
import unittest

from src.core.store import atomic
from src.core.store.logs import (
    HOME_PLACEHOLDER,
    MAX_STRING_LENGTH,
    ROOT_PLACEHOLDER,
    JsonlLogger,
    mask_path,
)

MOMENT = datetime.datetime(2026, 1, 20, 12, 0, tzinfo=datetime.timezone.utc)


class JsonlLoggerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logs_dir = os.path.join(self.tmp.name, "logs")
        self.logger = JsonlLogger(self.logs_dir)

    def _records(self, day="2026-01-20"):
        return self.logger.read(day)

    def test_daily_file_name(self):
        self.assertTrue(self.logger.log("scan_start", now=MOMENT))
        path = os.path.join(self.logs_dir, "2026-01-20.log")
        self.assertEqual(self.logger.last_written_path, path)
        self.assertTrue(os.path.isfile(path))

    def test_records_are_jsonl_with_time_and_event(self):
        self.logger.log("scan_start", now=MOMENT, tool_id="demo")
        records = self._records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["event"], "scan_start")
        self.assertEqual(records[0]["time"], "2026-01-20T12:00:00Z")
        self.assertEqual(records[0]["tool_id"], "demo")

    def test_unknown_fields_are_dropped(self):
        self.logger.log(
            "scan_start",
            now=MOMENT,
            token="secret",
            authorization="Bearer x",
            payload={"file": "content"},
        )
        record = self._records()[0]
        self.assertNotIn("token", record)
        self.assertNotIn("authorization", record)
        self.assertNotIn("payload", record)

    def test_containers_are_rendered_as_type_names(self):
        self.logger.log("scan_start", now=MOMENT, error={"code": 1})
        self.assertEqual(self._records()[0]["error"], "<dict>")

    def test_long_strings_are_clipped(self):
        self.logger.log("scan_start", now=MOMENT, error="x" * (MAX_STRING_LENGTH + 5))
        clipped = self._records()[0]["error"]
        self.assertEqual(len(clipped), MAX_STRING_LENGTH + 3)
        self.assertTrue(clipped.endswith("..."))

    def test_bytes_are_not_serialised(self):
        self.logger.log("scan_start", now=MOMENT, error=b"\x00\x01")
        self.assertEqual(self._records()[0]["error"], "<bytes:2>")

    def test_path_masking_uses_roots_and_home(self):
        home = os.path.expanduser("~")
        logger = JsonlLogger(
            self.logs_dir, mask_paths=True, roots=[os.path.join(self.tmp.name, "root")]
        )
        logger.log(
            "copy",
            now=MOMENT,
            source=os.path.join(self.tmp.name, "root", "data", "Cache"),
            target=os.path.join(home, "Cache"),
        )
        record = self._records()[0]
        self.assertEqual(record["source"], ROOT_PLACEHOLDER + os.sep + "data" + os.sep + "Cache")
        self.assertTrue(record["target"].startswith(HOME_PLACEHOLDER))

    def test_mask_path_handles_none(self):
        self.assertEqual(mask_path(None), "")

    def test_rotate_removes_only_old_daily_files(self):
        self.logger.log("x", now=MOMENT)
        for name in ("2026-01-05.log", "2026-01-06.log", "2026-01-19.log", "keep.txt"):
            with open(os.path.join(self.logs_dir, name), "wb") as handle:
                handle.write(b"")

        removed = self.logger.rotate(now=MOMENT)
        self.assertEqual(removed, ["2026-01-05.log"])
        remaining = sorted(os.listdir(self.logs_dir))
        self.assertIn("2026-01-06.log", remaining)
        self.assertIn("2026-01-19.log", remaining)
        self.assertIn("2026-01-20.log", remaining)
        self.assertIn("keep.txt", remaining)

    def test_rotate_tolerates_missing_directory(self):
        self.assertEqual(self.logger.rotate(now=MOMENT), [])

    def test_read_ignores_bad_and_blank_lines(self):
        self.logger.log("one", now=MOMENT)
        path = self.logger.path_for(MOMENT)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n")
            handle.write("not json\n")
            handle.write('["array"]\n')
        self.logger.log("two", now=MOMENT)

        records = self._records()
        self.assertEqual([record["event"] for record in records], ["one", "two"])

    def test_read_limit_returns_newest(self):
        for index in range(5):
            self.logger.log("event%d" % index, now=MOMENT)
        records = self.logger.read("2026-01-20", limit=2)
        self.assertEqual([record["event"] for record in records], ["event3", "event4"])

    def test_read_missing_day_returns_empty(self):
        self.assertEqual(self.logger.read("2020-01-01"), [])

    def test_log_failure_returns_false_without_raising(self):
        blocker = os.path.join(self.tmp.name, "blocker")
        with open(blocker, "wb") as handle:
            handle.write(b"x")
        logger = JsonlLogger(os.path.join(blocker, "logs"))
        self.assertFalse(logger.log("scan_start", now=MOMENT))
        self.assertTrue(logger.last_error)

    def test_is_available(self):
        self.assertTrue(self.logger.is_available())
        blocker = os.path.join(self.tmp.name, "blocker2")
        with open(blocker, "wb") as handle:
            handle.write(b"x")
        self.assertFalse(
            JsonlLogger(os.path.join(blocker, "logs")).is_available()
        )

    def test_empty_directory_is_rejected(self):
        with self.assertRaises(atomic.StoreError):
            JsonlLogger("")

    def test_records_are_one_line_each(self):
        self.logger.log("one", now=MOMENT)
        self.logger.log("two", now=MOMENT)
        path = self.logger.path_for(MOMENT)
        with open(path, "r", encoding="utf-8") as handle:
            lines = [line for line in handle if line.strip()]
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertIsInstance(json.loads(line), dict)


if __name__ == "__main__":
    unittest.main()
