# -*- coding: utf-8 -*-
"""AppService migration journal wiring (T12-F).

A migration journal must record the per-job backup directory so rollback can
locate ``manifest.json``; recording the configurable backup root made restore
report "backup manifest is missing".
"""

import os
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.core.store.journal import TYPE_MIGRATION  # noqa: E402
from tests.app_fakes import make_item, make_service  # noqa: E402

PER_JOB_BACKUP = r"D:\DiskWarden\backup\2026-10-20_161403_demo_41f1cf8f"


class _Job(object):
    job_id = "runner-job-1"


class _Token(object):
    def is_set(self):
        return False


class _Prepared(object):
    ok = False
    error_code = "COPY_FAILED"
    error_message = "injected copy failure"
    backup_dir = PER_JOB_BACKUP

    def to_dict(self):
        return {"error_code": self.error_code, "backup_dir": self.backup_dir}


class MigrateJournalTests(unittest.TestCase):
    def test_journal_records_the_per_job_backup_dir(self):
        service = make_service(self)
        item = make_item(source_path=r"C:\Users\alice\AppData\Roaming\Cursor\Cache")

        with mock.patch(
            "src.app.service.prepare_copy", return_value=_Prepared()
        ):
            result = service._migrate_one(
                _Job(), _Token(), lambda *args, **kwargs: None, 0, 1, item
            )

        self.assertFalse(result["ok"])
        entries = service.journals.list_all()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.type, TYPE_MIGRATION)
        self.assertEqual(entry.backup_dir, PER_JOB_BACKUP)


if __name__ == "__main__":
    unittest.main()
