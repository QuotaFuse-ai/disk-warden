# -*- coding: utf-8 -*-
"""Hard-interrupt matrix: swap / link / link_verify / cleanup (T13).

Each case runs a real migration in a child process that kills itself
(``os._exit``) at one stage boundary, then starts a *fresh* ``AppService`` on
the same data root — the equivalent of relaunching the app after a crash — and
requires that:

* the unfinished job is reported by ``recovery_list`` with both a recover and
  a rollback entry;
* the recovery run reaches a terminal state;
* the user data is intact (file count, total bytes and every SHA-256) and the
  IDE directory is still usable.
"""

import json
import os
import time
import unittest

from src.core.store.journal import STATE_DONE, STATE_FAILED, STATE_ROLLED_BACK
from tests.e2e import support

REPO_ROOT = support.REPO_ROOT

# What each interruption point must converge to.  ``source``/``final`` describe
# the expected shape *after* recovery; ``cache_at`` says where the bytes live.
EXPECTATIONS = {
    "swap": {
        "states": (STATE_FAILED,),
        "source": "dir",
        "final": "missing",
        "reason": "source was never moved; the promoted target is removed",
    },
    "link": {
        "states": (STATE_ROLLED_BACK,),
        "source": "dir",
        "final": "missing",
        "reason": "only the tombstone existed; the source is restored",
    },
    "link_verify": {
        "states": (STATE_DONE,),
        "source": "link",
        "final": "dir",
        "reason": "the junction is valid; recovery finishes the cleanup",
    },
    "cleanup": {
        "states": (STATE_DONE,),
        "source": "link",
        "final": "dir",
        "reason": "the junction was verified; recovery removes the tombstone",
    },
}

RESIDUE_PREFIXES = (".dw-partial-", ".dwtombstone-", ".dw-restore-")


class HardKillMatrix(support.SandboxTestCase):
    """One sandbox per interruption point; the profile follows the disk."""

    def _run_kill_child(self, stage):
        # type: (str) -> None
        support.run_kill_child(self, self.sandbox, stage)

    def _check_matrix(self, stage):
        # type: (str) -> None
        sandbox = self.sandbox
        expectation = EXPECTATIONS[stage]
        before_cache = support.tree_state(sandbox.cache_path)
        before_tool = support.tree_state(sandbox.tool_root)
        self.assertTrue(before_cache, "fixture cache is empty")
        fixture_bytes = sum(item["size"] for item in before_cache.values())
        self.log(
            "fixture",
            stage=stage,
            scale=sandbox.scale,
            bytes=fixture_bytes,
            files=len(before_cache),
            depth=support.tree_depth(sandbox.cache_path),
        )

        killed_at = time.time()
        self._run_kill_child(stage)
        kill_seconds = round(time.time() - killed_at, 2)

        # -- restart: a brand new service must see the unfinished job -----
        service = self.service_for()
        pending = service.recovery_list()
        self.assertEqual(pending["count"], 1, "restart did not report the job: %r" % (pending,))
        item = pending["items"][0]
        self.assertEqual(item["type"], "migration")
        self.assertEqual(
            os.path.normcase(os.path.abspath(item["source_path"])),
            os.path.normcase(os.path.abspath(sandbox.cache_path)),
        )
        self.assertIn("recover", item["actions"], "no recover entry: %r" % (item,))
        self.assertIn("rollback", item["actions"], "no rollback entry: %r" % (item,))
        job_id = item["job_id"]
        self.log("restart detected job", job_id=job_id, state=item["state"], stage=stage)

        # The rollback entry point must answer coherently from this state.
        preview = service.rollback_preview(job_id)
        self.assertIn("can_proceed", preview)
        self.log("rollback preview", stage=stage, preview=preview.get("mode"), can=preview.get("can_proceed"))

        # -- recover ------------------------------------------------------
        recover_started_at = time.time()
        started = service.recovery_run(job_id, "recover")
        summary = service.runner.wait(started["job_id"], timeout=1800.0)
        recover_seconds = round(time.time() - recover_started_at, 2)
        self.assertEqual(summary.get("state"), "done", "recovery job failed: %r" % (summary,))
        result = summary.get("summary") or {}
        state = result.get("state")
        self.log(
            "recovery result",
            stage=stage,
            state=state,
            kill_seconds=kill_seconds,
            recover_seconds=recover_seconds,
            result=result,
        )
        self.assertIn(
            state,
            expectation["states"],
            "stage %s converged to %r, expected %r (%s)"
            % (stage, state, expectation["states"], expectation["reason"]),
        )

        # -- shape --------------------------------------------------------
        source_kind = _kind(sandbox.cache_path)
        final_path = item["final_path"]
        final_kind = _kind(final_path)
        self.assertEqual(source_kind, expectation["source"], "source shape at %s" % stage)
        self.assertEqual(final_kind, expectation["final"], "target shape at %s" % stage)

        if expectation["source"] == "link":
            from src.platform import read_junction_target

            target = read_junction_target(sandbox.cache_path)
            self.assertEqual(
                os.path.normcase(os.path.abspath(target)),
                os.path.normcase(os.path.abspath(final_path)),
                "junction points at the wrong target",
            )
            self.assert_data_equal(
                before_cache, support.tree_state(sandbox.cache_path),
                "cache read through the junction",
            )
            # The IDE root snapshot loses the cache subtree (it is a link now)
            # but must otherwise be byte-identical.
            after_tool = support.tree_state(sandbox.tool_root)
            expected_tool = {
                key: value
                for key, value in before_tool.items()
                if not key.startswith("Cache/")
            }
            self.assert_data_equal(expected_tool, after_tool, "IDE root")
        else:
            self.assert_data_equal(
                before_cache, support.tree_state(sandbox.cache_path), "source tree"
            )
            self.assert_data_equal(
                before_tool, support.tree_state(sandbox.tool_root), "IDE root"
            )

        self._assert_no_residue(os.path.dirname(sandbox.cache_path))
        self._assert_target_is_real_dir(final_path, expect=expectation["final"])
        self._assert_ide_still_usable()
        self.log(
            "integrity verified",
            stage=stage,
            source=source_kind,
            final=final_kind,
            bytes=sum(item["size"] for item in support.tree_state(sandbox.cache_path).values()),
        )

        self.assertEqual(
            service.recovery_list()["count"], 0, "the job is still pending after recovery"
        )

    # -- assertions -------------------------------------------------------

    def assert_data_equal(self, before, after, label):
        # type: (dict, dict, str) -> None
        try:
            support.assert_data_equal(self, before, after, label)
        except AssertionError as exc:
            self.log("data mismatch", label=label, detail=str(exc)[:2000])
            raise

    def _assert_no_residue(self, directory):
        # type: (str) -> None
        names = []
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name.startswith(RESIDUE_PREFIXES):
                    names.append(entry.name)
        self.assertEqual(names, [], "temporary directories left behind: %r" % (names,))

    def _assert_target_is_real_dir(self, final_path, expect="dir"):
        # type: (str, str) -> None
        from src.platform import is_junction

        if expect == "missing":
            self.assertFalse(
                os.path.exists(final_path), "target still exists: %s" % final_path
            )
            return
        self.assertTrue(os.path.isdir(final_path), "target missing: %s" % final_path)
        self.assertFalse(
            is_junction(final_path), "target must be a real directory, not a link"
        )

    def _assert_ide_still_usable(self):
        # type: () -> None
        sandbox = self.sandbox
        self.assertTrue(os.path.isfile(sandbox.exe_path), "Cursor.exe disappeared")
        self.assertTrue(support.probe_read(sandbox.exe_path), "Cursor.exe unreadable")
        with open(sandbox.settings_path, "r", encoding="utf-8") as handle:
            settings = json.load(handle)
        self.assertEqual(settings.get("dw"), "sentinel", "settings.json was altered")
        self.assertGreater(support.tree_depth(sandbox.cache_path), 3, "cache tree is flat")

    # -- cases ------------------------------------------------------------

    def test_swap_before_source_rename(self):
        self._check_matrix("swap")

    def test_link_after_source_renamed(self):
        self._check_matrix("link")

    def test_link_verify_after_junction_created(self):
        self._check_matrix("link_verify")

    def test_cleanup_after_link_verified(self):
        self._check_matrix("cleanup")


def _kind(path):
    # type: (str) -> str
    from src.platform import is_junction

    if not os.path.exists(path):
        return "missing"
    try:
        if is_junction(path) or os.path.islink(path):
            return "link"
    except OSError:
        pass
    return "dir" if os.path.isdir(path) else "other"


if __name__ == "__main__":
    unittest.main()
