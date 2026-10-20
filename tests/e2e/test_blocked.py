# -*- coding: utf-8 -*-
"""Scenario 6: the guards that must stop a migration before it starts.

Covers the acceptance list: permission denied, insufficient space, the IDE
still running, and red (protected) paths.  Every refusal must leave the
profile untouched, and none of them may create a journal entry.
"""

import os
import unittest

from src.app.errors import BridgeError
from tests.e2e import support


class Blocked(support.SandboxTestCase):
    def _scanned(self, **overrides):
        service = self.service_for(**overrides)
        snapshot = support.run_scan(self, service)
        return service, snapshot, support.find_item(snapshot, self.sandbox.cache_path)

    def test_insufficient_space_is_refused_before_any_copy(self):
        service, _snapshot, item = self._scanned(free_space_provider=lambda path=None: 1024)
        before = support.tree_state(self.sandbox.cache_path)

        with self.assertRaises(BridgeError) as caught:
            support.plan(self, service, [item["item_id"]])
        error = caught.exception
        self.assertEqual(error.code, "INSUFFICIENT_SPACE")
        detail = error.detail or {}
        self.assertEqual(detail.get("free_bytes"), 1024)
        self.assertGreater(int(detail.get("required_bytes") or 0), 1024)
        self.log("space guard", detail=detail)

        support.assert_data_equal(self, before, support.tree_state(self.sandbox.cache_path), "source")
        self.assertEqual(service.journals.list_all(), [])
        self.assertFalse(os.path.exists(self.sandbox.target_root + os.sep + "cursor" + os.sep + "Cache"))

    def test_a_running_ide_blocks_the_plan(self):
        service, _snapshot, item = self._scanned(process_provider=support.busy_process_provider())
        before = support.tree_state(self.sandbox.cache_path)

        with self.assertRaises(BridgeError) as caught:
            support.plan(self, service, [item["item_id"]])
        error = caught.exception
        self.assertEqual(error.code, "BLOCKED_PROCESS")
        self.assertIn("close", (error.reason or "").lower())
        items = (error.detail or {}).get("items") or []
        self.assertTrue(items, "the refusal did not name the conflicting process")
        self.assertEqual(items[0]["tool_id"], support.TOOL_ID)
        self.log("process guard", process=items[0].get("process"))

        support.assert_data_equal(self, before, support.tree_state(self.sandbox.cache_path), "source")
        self.assertEqual(service.journals.list_all(), [])

    def test_unreadable_directory_blocks_the_plan(self):
        """A partial scan must refuse to plan instead of migrating half a tree."""
        sandbox = self.sandbox
        locked = os.path.join(sandbox.cache_path, "Cache_Data", "level1")
        service = self.service_for()

        with support.deny_scandir(locked):
            snapshot = support.run_scan(self, service)
        item = support.find_item(snapshot, sandbox.cache_path)
        self.assertGreaterEqual(item["access_errors"], 1, "the scan hid a permission error")
        self.assertGreater(int(snapshot["stats"]["access_errors"]), 0)
        self.log("scan saw a permission error", access_errors=item["access_errors"])

        with self.assertRaises(BridgeError) as caught:
            support.plan(self, service, [item["item_id"]])
        self.assertEqual(caught.exception.code, "PERMISSION_DENIED")
        self.assertTrue((caught.exception.detail or {}).get("items"))
        self.assertEqual(service.journals.list_all(), [])

        # Once the directory is readable again the same item plans cleanly.
        healed = support.run_scan(self, service)
        healed_item = support.find_item(healed, sandbox.cache_path)
        self.assertEqual(healed_item["access_errors"], 0)
        plan_payload = support.plan(self, service, [healed_item["item_id"]])
        self.assertTrue(plan_payload.get("plan_id"))
        support.assert_inside_sandbox(sandbox, plan_payload["items"][0]["final_path"], "final path")

    def test_red_path_cannot_be_planned_even_with_confirmation(self):
        service, snapshot, _item = self._scanned()
        red = [
            entry
            for entry in snapshot["items"]
            if entry.get("safety") == "red"
            and os.path.normcase(entry["source_path"]).endswith(os.path.normcase("settings.json"))
        ]
        self.assertTrue(red, "the fixture did not expose settings.json as a red item")
        target = red[0]

        with self.assertRaises(BridgeError) as caught:
            service.plan_preview([target["item_id"]], [target["item_id"]])
        self.assertEqual(caught.exception.code, "BLOCKED_RED_PATH")
        self.assertFalse(target["can_migrate"])
        self.assertEqual(service.journals.list_all(), [])
        self.log("red guard", path=target["source_path"])

    def test_starting_the_ide_after_planning_stops_the_swap(self):
        """The IDE may be launched between planning and the swap; S5 must notice."""
        sandbox = self.sandbox
        running = {"busy": False}

        def provider():
            return support.busy_process_provider()() if running["busy"] else []

        service, _snapshot, item = self._scanned(process_provider=provider)
        before = support.tree_state(sandbox.cache_path)
        plan_payload = support.plan(self, service, [item["item_id"]])

        running["busy"] = True
        job = support.start_migration(self, service, plan_payload)
        self.assertEqual(job.get("state"), "done", "job crashed: %r" % (job,))
        summary = job.get("summary") or {}
        self.assertEqual(summary.get("failed"), 1)
        result = summary["items"][0]
        self.assertFalse(result["ok"])
        self.log("swap refused", state=result["state"], reason=result.get("message"))

        # The source was never renamed and no final tree was published.
        support.assert_real_dir(self, sandbox.cache_path, "source")
        support.assert_data_equal(self, before, support.tree_state(sandbox.cache_path), "source")
        self.assertFalse(os.path.exists(result["target_path"]), "the final tree was published")
        support.assert_no_residue(self, sandbox.tool_root)
        support.ide_still_usable(self, sandbox)
        self.assertEqual(service.recovery_list()["count"], 0)


if __name__ == "__main__":
    unittest.main()
