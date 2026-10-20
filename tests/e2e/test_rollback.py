# -*- coding: utf-8 -*-
"""Scenario 5: rollback restores the original directory (T13).

Acceptance (release hardening): after a rollback the original path must be a
real directory again, not a link; file count, total size and per-file hashes
must match the pre-migration state; the IDE must still start; the final tree
must be gone.
"""

import os
import unittest

from src.core.store.journal import STATE_DONE, STATE_ROLLED_BACK, TYPE_MIGRATION, TYPE_ROLLBACK
from tests.e2e import support


class Rollback(support.SandboxTestCase):
    def _migrate(self):
        sandbox = self.sandbox
        before = support.tree_state(sandbox.cache_path)
        service = self.service_for()
        snapshot = support.run_scan(self, service)
        item = support.find_item(snapshot, sandbox.cache_path)
        job = support.start_migration(
            self, service, support.plan(self, service, [item["item_id"]])
        )
        self.assertEqual((job.get("summary") or {}).get("succeeded"), 1, "setup failed")
        job_id = support.migration_job_id(service, sandbox.cache_path)
        target = job["summary"]["items"][0]["target_path"]
        return sandbox, service, job_id, target, before

    def test_rollback_restores_a_real_directory_with_identical_bytes(self):
        sandbox, service, job_id, target, before = self._migrate()

        preview = service.rollback_preview(job_id)
        self.assertTrue(preview["can_proceed"], "rollback was blocked: %r" % (preview,))
        self.assertEqual(preview["mode"], "rollback")
        self.assertEqual(
            os.path.normcase(os.path.abspath(preview["final_path"])),
            os.path.normcase(os.path.abspath(target)),
        )
        self.log("rollback preview", mode=preview["mode"], restore_bytes=preview["restore_bytes"])

        job = support.start_rollback(self, service, job_id)
        self.assertEqual(job.get("state"), "done", "rollback job crashed: %r" % (job,))
        summary = job.get("summary") or {}
        self.assertTrue(summary.get("ok"), "rollback failed: %r" % (summary,))
        self.assertEqual(summary.get("state"), STATE_ROLLED_BACK)
        self.assertTrue(summary.get("source_restored"))
        self.assertTrue(summary.get("target_removed"))
        self.assertTrue(summary.get("data_kept"))

        # -- the original path is a real directory again --------------------
        support.assert_real_dir(self, sandbox.cache_path, "restored source")
        self.assertFalse(os.path.exists(target), "the final tree survived the rollback")
        support.assert_no_residue(self, sandbox.tool_root, sandbox.target_root)

        # -- and it holds exactly the bytes it held before ------------------
        after = support.tree_state(sandbox.cache_path)
        support.assert_data_equal(self, before, after, "restored cache")

        # -- journal / history / recovery ----------------------------------
        # A rollback is recorded in its own journal entry; the migration
        # journal keeps the state it reached (``DONE``) as an audit trail.
        migration_entry = service.journals.load(job_id, TYPE_MIGRATION)
        self.assertEqual(migration_entry.state, STATE_DONE)
        rollback_entry = service.journals.load(job_id, TYPE_ROLLBACK)
        self.assertIsNotNone(rollback_entry, "the rollback wrote no journal entry")
        self.assertEqual(rollback_entry.state, STATE_ROLLED_BACK)
        record = support.history_record(service, job_id)
        self.assertEqual(record["raw_result"], "rolled_back")
        self.assertEqual(service.recovery_list()["count"], 0)

        # -- the profile still works ---------------------------------------
        support.ide_still_usable(self, sandbox)

    def test_rollback_preview_refuses_a_finished_rollback(self):
        sandbox, service, job_id, target, before = self._migrate()
        support.start_rollback(self, service, job_id)

        # Nothing is left to roll back: the preview reports the terminal mode
        # and must not hand out a plan (a second rollback would be a no-op
        # that could still delete data if it were allowed to run).
        again = service.rollback_preview(job_id)
        self.assertEqual(again["mode"], "already_rolled_back")
        self.assertFalse(again["can_proceed"])
        self.assertNotIn("plan_id", again, "a finished rollback still produced a plan")
        self.log("second preview", mode=again["mode"], can=again["can_proceed"])
        # Previewing it touched nothing.
        support.assert_real_dir(self, sandbox.cache_path, "restored source")
        support.assert_data_equal(self, before, support.tree_state(sandbox.cache_path), "restored cache")

    def test_rolled_back_item_requires_confirmation_before_another_migration(self):
        sandbox, service, job_id, target, before = self._migrate()
        support.start_rollback(self, service, job_id)

        # ``_apply_rollback_to_snapshot`` demotes the item to yellow/confirm.
        snapshot = service.scan_result()
        item = support.find_item(snapshot, sandbox.cache_path)
        self.assertTrue(item["requires_confirm"], "a rolled back item became silently migratable again")
        self.assertEqual(item["safety"], "red")
        self.log("post-rollback item", safety=item["safety"], reason=item.get("reason"))

        # A red item may never be re-migrated, even with confirmation.
        from src.app.errors import BridgeError

        with self.assertRaises(BridgeError) as caught:
            service.plan_preview([item["item_id"]], [item["item_id"]])
        self.assertEqual(caught.exception.code, "BLOCKED_RED_PATH")
        support.ide_still_usable(self, sandbox)


if __name__ == "__main__":
    unittest.main()
