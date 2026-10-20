# -*- coding: utf-8 -*-
"""Scenario 2: a migration that succeeds end to end (T13).

Acceptance: the source becomes a junction, the final tree is byte-identical,
no temporary leftovers remain, history and journal agree, and the IDE profile
is still loadable.
"""

import os
import unittest

from src.core.migrate.models import STATE_DONE
from tests.e2e import support


class MigrateSuccess(support.SandboxTestCase):
    def test_migrate_moves_cache_and_leaves_a_working_link(self):
        sandbox = self.sandbox
        before = support.tree_state(sandbox.cache_path)
        tool_before = support.tree_state(sandbox.tool_root)
        self.assertTrue(before, "fixture did not build the cache tree")

        service = self.service_for()
        snapshot = support.run_scan(self, service)
        item = support.find_item(snapshot, sandbox.cache_path)
        self.assertEqual(item["safety"], "green")

        plan_payload = support.plan(self, service, [item["item_id"]])
        job = support.start_migration(self, service, plan_payload)
        self.assertEqual(job.get("state"), "done", "migration job did not finish: %r" % (job,))

        summary = job.get("summary") or {}
        self.assertEqual(summary.get("succeeded"), 1, "migration did not succeed: %r" % (summary,))
        self.assertEqual(summary.get("failed"), 0)
        self.assertFalse(summary.get("cancelled"))
        result = summary["items"][0]
        self.assertTrue(result["ok"], "item failed: %r" % (result,))
        self.assertEqual(result["state"], STATE_DONE)
        self.log("migrated", target=result["target_path"], bytes=result["bytes"])

        # -- source is now a junction onto the final tree -------------------
        target = result["target_path"]
        support.assert_junction_to(self, sandbox.cache_path, target)
        support.assert_real_dir(self, target, "final tree")

        # -- data survived byte for byte -----------------------------------
        after = support.tree_state(sandbox.cache_path)
        support.assert_data_equal(self, before, after, "cache through the junction")
        self.assertEqual(
            len(after) >= self.sandbox.profile["bulk_files"],
            True,
            "the file count collapsed",
        )

        # Everything outside Cache is untouched; Cache itself is now a link,
        # so ``tree_state`` skips its contents instead of walking the target.
        tool_after = support.tree_state(sandbox.tool_root)
        expected = {
            key: value
            for key, value in tool_before.items()
            if not key.startswith("Cache/")
        }
        support.assert_data_equal(self, expected, tool_after, "profile outside Cache")

        # -- no temporary leftovers ----------------------------------------
        support.assert_no_residue(self, sandbox.tool_root, sandbox.target_root)

        # -- journal, history and recovery agree ---------------------------
        job_id = support.migration_job_id(service, sandbox.cache_path)
        entry = service.journals.load(job_id, "migration")
        self.assertIsNotNone(entry, "journal entry disappeared")
        self.assertEqual(entry.state, STATE_DONE)
        self.assertEqual(
            os.path.normcase(os.path.abspath(entry.final)),
            os.path.normcase(os.path.abspath(target)),
        )

        record = support.history_record(service, job_id)
        self.assertEqual(record["raw_result"], "success")
        self.assertGreater(int(record["freed"]), 0)
        self.assertEqual(service.recovery_list()["count"], 0, "a finished job was reported as pending")

        # -- the profile still works ---------------------------------------
        support.ide_still_usable(self, sandbox)

    def test_migrated_item_is_retired_from_the_next_plan(self):
        sandbox = self.sandbox
        service = self.service_for()
        snapshot = support.run_scan(self, service)
        item = support.find_item(snapshot, sandbox.cache_path)
        plan_payload = support.plan(self, service, [item["item_id"]])
        support.start_migration(self, service, plan_payload)

        # The in-memory snapshot marks the item as migrated, so a second
        # selection must be refused instead of silently re-copying.
        from src.app.errors import BridgeError

        with self.assertRaises(BridgeError) as caught:
            service.plan_preview([item["item_id"]])
        self.assertIn(
            caught.exception.code,
            ("ALREADY_MIGRATED", "BLOCKED_RED_PATH", "DUPLICATE_TARGET", "INVALID_REQUEST"),
            "unexpected guard: %s (%s)" % (caught.exception.code, caught.exception.reason),
        )
        self.log("second plan refused", code=caught.exception.code)

    def test_migrated_tree_is_still_scannable_after_a_restart(self):
        """A fresh service must see the link, not walk it or re-offer it."""
        sandbox = self.sandbox
        service = self.service_for()
        snapshot = support.run_scan(self, service)
        item = support.find_item(snapshot, sandbox.cache_path)
        support.start_migration(self, service, support.plan(self, service, [item["item_id"]]))
        service.close()

        restarted = self.service_for()
        again = support.run_scan(self, restarted)
        cache = support.find_item(again, sandbox.cache_path)
        self.assertTrue(cache["is_reparse"], "the scanner did not recognise the junction")
        self.assertFalse(cache["can_migrate"], "a migrated item is offered again")
        self.assertEqual(restarted.recovery_list()["count"], 0)
        self.assertLess(
            int(cache["bytes"]),
            sum(size for size, _ in sandbox.profile["blobs"]),
            "the scanner walked into the junction target",
        )


if __name__ == "__main__":
    unittest.main()
