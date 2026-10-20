# -*- coding: utf-8 -*-
"""Scenario 4: restart after an interrupted migration (T13).

The migration is killed with ``os._exit`` before the source is renamed, i.e.
the safest window: the copy is verified but nothing has been swapped yet.
After a restart the app must report the job, offer both recover and rollback,
and never delete the leftover copy without an explicit confirmation.
"""

import os
import unittest

from src.app.errors import BridgeError
from src.core.rollback.models import MODE_RESIDUAL_COPY
from src.core.store.journal import STATE_COPY_VERIFIED, STATE_ROLLED_BACK, STATE_RESIDUAL_COPY
from tests.e2e import support


class Recovery(support.SandboxTestCase):
    def _interrupt_before_rename(self):
        sandbox = self.sandbox
        before = support.tree_state(sandbox.cache_path)
        support.run_kill_child(self, sandbox, "swap")
        return before

    def test_restart_reports_the_unfinished_job(self):
        self._interrupt_before_rename()
        service = self.service_for()

        pending = service.recovery_list()
        self.assertEqual(pending["count"], 1, "restart missed the unfinished job: %r" % (pending,))
        item = pending["items"][0]
        self.assertEqual(item["state"], STATE_COPY_VERIFIED)
        self.assertEqual(sorted(item["actions"]), ["recover", "rollback"])
        self.assertFalse(item["needs_human"], "a plain interruption must not require a human")
        self.assertTrue(item["label"])
        # ``AppService`` caches this list at startup for the UI banner.
        self.assertEqual(
            [entry["job_id"] for entry in service._last_recovery], [item["job_id"]]
        )
        self.log("pending job", state=item["state"], actions=item["actions"])

    def test_recovery_rollback_keeps_the_unconfirmed_copy(self):
        before = self._interrupt_before_rename()
        service = self.service_for()
        job_id = service.recovery_list()["items"][0]["job_id"]

        # The implicit rollback refuses to delete the leftover target copy.
        started = service.recovery_run(job_id, "rollback")
        summary = service.runner.wait(started["job_id"], timeout=1800.0).get("summary") or {}
        self.assertEqual(summary.get("state"), STATE_RESIDUAL_COPY)
        self.assertTrue(summary.get("data_kept"))

        # The source is a real directory and still holds every byte.
        support.assert_real_dir(self, self.sandbox.cache_path, "source")
        support.assert_data_equal(self, before, support.tree_state(self.sandbox.cache_path), "source")
        support.ide_still_usable(self, self.sandbox)

        # The preview names the leftover copy and refuses to proceed.
        preview = service.rollback_preview(job_id)
        self.assertEqual(preview["mode"], MODE_RESIDUAL_COPY)
        self.assertFalse(preview["can_proceed"], "an unconfirmed residual copy was approved")
        self.assertNotIn("plan_id", preview, "a blocked preview still handed out a plan")
        self.log("residual preview", state=preview["mode"], bytes_=preview["restore_bytes"])

    @unittest.expectedFailure
    def test_residual_preview_asks_for_confirmation(self):
        """DEF-04: the preview must flag that a confirmation is needed.

        ``rollback_preview`` compares ``precheck.mode`` (``"residual_copy"``)
        with ``STATE_RESIDUAL_COPY`` (``"RESIDUAL_COPY"``), two different
        namespaces, so ``requires_residual_confirm`` is always ``False`` —
        the UI can never tell the user why the rollback is held back.
        """
        self._interrupt_before_rename()
        service = self.service_for()
        job_id = service.recovery_list()["items"][0]["job_id"]
        service.runner.wait(
            service.recovery_run(job_id, "rollback")["job_id"], timeout=1800.0
        )
        self.assertTrue(service.rollback_preview(job_id)["requires_residual_confirm"])

    def test_confirmed_residual_rollback_removes_the_copy(self):
        before = self._interrupt_before_rename()
        service = self.service_for()
        job_id = service.recovery_list()["items"][0]["job_id"]
        service.runner.wait(
            service.recovery_run(job_id, "rollback")["job_id"], timeout=1800.0
        )

        # DEF-01: the confirmed path is driven one layer down, because
        # ``rollback_preview(confirm_residual_delete=True)`` crashes on a
        # frozen dataclass field (see ``test_confirmed_preview_is_reachable``).
        result = support.residual_rollback(self, service, job_id)
        self.assertTrue(result.target_removed, "the leftover copy was not removed")
        target = service.journals.load(job_id, "migration").final
        self.assertFalse(os.path.exists(target), "the leftover copy survived")
        support.assert_no_residue(self, self.sandbox.tool_root, self.sandbox.target_root)
        support.assert_data_equal(self, before, support.tree_state(self.sandbox.cache_path), "source")
        self.log("residual cleanup", state=result.state, ok=result.ok)

    @unittest.expectedFailure
    def test_confirmed_preview_is_reachable(self):
        """DEF-01: the confirmed residual path must work through the bridge.

        ``RollbackRequest`` is ``@dataclass(frozen=True)`` but
        ``AppService.rollback_preview`` assigns to
        ``request.confirm_residual_delete`` (service.py:1571), so the user
        cannot confirm the cleanup of a copy orphaned by a crash.
        """
        self._interrupt_before_rename()
        service = self.service_for()
        job_id = service.recovery_list()["items"][0]["job_id"]
        service.runner.wait(
            service.recovery_run(job_id, "rollback")["job_id"], timeout=1800.0
        )
        preview = service.rollback_preview(job_id, confirm_residual_delete=True)
        self.assertTrue(preview["can_proceed"])

    @unittest.expectedFailure
    def test_residual_cleanup_reports_a_terminal_state(self):
        """DEF-03: a successful residual cleanup must be reported as done.

        The engine's residual branch sets ``target_removed`` and writes a
        journal entry, but never assigns ``result.state``, so ``ok`` stays
        ``False`` and the job is never closed out — the UI shows a failure
        (and ``recovery_list`` keeps listing the job) after the copy was
        actually deleted.
        """
        self._interrupt_before_rename()
        service = self.service_for()
        job_id = service.recovery_list()["items"][0]["job_id"]
        service.runner.wait(
            service.recovery_run(job_id, "rollback")["job_id"], timeout=1800.0
        )
        result = support.residual_rollback(self, service, job_id)
        self.assertTrue(result.ok, "residual cleanup reported %r" % (result.state,))
        self.assertEqual(result.state, STATE_ROLLED_BACK)

    def test_recovery_run_validates_its_input(self):
        service = self.service_for()
        with self.assertRaises(BridgeError) as caught:
            service.recovery_run("0123456789abcdef", "recover")
        self.assertEqual(caught.exception.code, "NOT_FOUND")

        with self.assertRaises(BridgeError) as caught:
            service.recovery_run("0123456789abcdef", "explode")
        self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")

        with self.assertRaises(BridgeError) as caught:
            service.recovery_run("", "recover")
        self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")

    @unittest.expectedFailure
    def test_malformed_job_id_is_rejected_as_a_bridge_error(self):
        """DEF-02: a malformed journal id must not leak ``StoreError``.

        ``JournalStore.path_for`` validates the id and raises the core
        ``StoreError`` outside the bridge envelope, so the UI receives an
        unhandled exception instead of ``INVALID_ARGUMENT``.
        """
        service = self.service_for()
        service.recovery_run("not-a-job", "recover")

    def test_clean_restart_reports_nothing(self):
        service = self.service_for()
        self.assertEqual(service.recovery_list(), {"items": [], "count": 0, "warnings": []})
        service.close()
        restarted = self.service_for()
        self.assertEqual(restarted.recovery_list()["count"], 0)


if __name__ == "__main__":
    unittest.main()
