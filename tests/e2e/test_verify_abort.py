# -*- coding: utf-8 -*-
"""Scenario 3: the verifier aborts a migration without touching the source.

Two families of failure are covered:

* S4 verification mismatch (the copy landed but is not identical);
* a copy that dies with ``PermissionError`` (elevation/AV lock).

Both must leave the profile exactly as it was, remove the staging tree, and
never publish a recovery entry.
"""

import os
import unittest

from src.core.migrate.models import STATE_FAILED
from tests.e2e import support


def _damage_one_file(source_path, staging_path):
    """Append a byte to the largest copied file so the hash no longer matches."""
    victim = None
    for current, _dirnames, filenames in os.walk(staging_path):
        for name in filenames:
            path = os.path.join(current, name)
            if victim is None or os.path.getsize(path) > os.path.getsize(victim):
                victim = path
    if victim is None:
        raise AssertionError("stand-in transfer produced no files")
    with open(victim, "ab") as handle:
        handle.write(b"tampered")


def _denied_transfer(source_path, staging_path, log_path, cancel_token=None):
    raise PermissionError(13, "Access is denied", staging_path)


class VerifyAbort(support.SandboxTestCase):
    def _migrate_with(self, transfer):
        sandbox = self.sandbox
        before = support.tree_state(sandbox.cache_path)
        service = self.service_for(transfer=transfer)
        snapshot = support.run_scan(self, service)
        item = support.find_item(snapshot, sandbox.cache_path)
        plan_payload = support.plan(self, service, [item["item_id"]])
        job = support.start_migration(self, service, plan_payload)
        self.assertEqual(job.get("state"), "done", "migration job crashed: %r" % (job,))
        return sandbox, service, item, before, job

    def test_verification_mismatch_aborts_and_keeps_the_source(self):
        sandbox, service, item, before, job = self._migrate_with(
            support.robocopy_like_transfer(corrupt=_damage_one_file)
        )
        summary = job.get("summary") or {}
        self.assertEqual(summary.get("failed"), 1, "the mismatch was not reported: %r" % (summary,))
        self.assertEqual(summary.get("succeeded"), 0)
        result = summary["items"][0]
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], STATE_FAILED)

        # The source is a real directory with the original bytes.
        support.assert_real_dir(self, sandbox.cache_path, "source")
        support.assert_data_equal(self, before, support.tree_state(sandbox.cache_path), "source")
        support.ide_still_usable(self, sandbox)

        # The staging tree is gone and no final tree was published.
        self.assertFalse(
            os.path.exists(result["target_path"]),
            "a failed migration left a final tree: %s" % result["target_path"],
        )
        support.assert_no_residue(self, sandbox.target_root, sandbox.tool_root)

        job_id = support.migration_job_id(service, sandbox.cache_path)
        entry = service.journals.load(job_id, "migration")
        self.assertEqual(entry.state, STATE_FAILED)
        self.assertEqual(entry.result.get("error_code"), "VERIFY_FAILED")
        self.log("verify abort", code=entry.result.get("error_code"))

        record = support.history_record(service, job_id)
        self.assertEqual(record["raw_result"], "failure")
        self.assertEqual(record["code"], "VERIFY_FAILED")

        # A clean failure is terminal: nothing to recover after a restart.
        self.assertEqual(service.recovery_list()["count"], 0)
        restarted = self.service_for()
        self.assertEqual(restarted.recovery_list()["count"], 0)

    def test_copy_permission_error_aborts_and_keeps_the_source(self):
        sandbox, service, item, before, job = self._migrate_with(_denied_transfer)
        summary = job.get("summary") or {}
        self.assertEqual(summary.get("failed"), 1)
        result = summary["items"][0]
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], STATE_FAILED)

        support.assert_real_dir(self, sandbox.cache_path, "source")
        support.assert_data_equal(self, before, support.tree_state(sandbox.cache_path), "source")
        self.assertFalse(os.path.exists(result["target_path"]), "target was created")
        support.assert_no_residue(self, sandbox.target_root, sandbox.tool_root)
        support.ide_still_usable(self, sandbox)

        job_id = support.migration_job_id(service, sandbox.cache_path)
        entry = service.journals.load(job_id, "migration")
        self.assertEqual(entry.state, STATE_FAILED)
        self.assertEqual(entry.result.get("error_code"), "COPY_FAILED")
        self.assertEqual(service.recovery_list()["count"], 0)

    def test_aborted_job_can_be_retried_successfully(self):
        """After a mismatch the user fixes nothing but retries: it must work."""
        sandbox, service, item, before, job = self._migrate_with(
            support.robocopy_like_transfer(corrupt=_damage_one_file)
        )
        self.assertEqual((job.get("summary") or {}).get("failed"), 1)

        healthy = self.service_for()
        snapshot = support.run_scan(self, healthy)
        again = support.find_item(snapshot, sandbox.cache_path)
        self.assertTrue(again["can_migrate"], "the aborted item is no longer migratable")
        retried = support.start_migration(
            self, healthy, support.plan(self, healthy, [again["item_id"]])
        )
        self.assertEqual(retried.get("state"), "done")
        self.assertEqual((retried.get("summary") or {}).get("succeeded"), 1)
        support.assert_junction_to(
            self, sandbox.cache_path, retried["summary"]["items"][0]["target_path"]
        )
        support.assert_data_equal(
            self, before, support.tree_state(sandbox.cache_path), "cache after retry"
        )
        self.assertEqual(healthy.recovery_list()["count"], 0)
        self.log("retry succeeded")


if __name__ == "__main__":
    unittest.main()
