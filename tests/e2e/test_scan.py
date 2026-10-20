# -*- coding: utf-8 -*-
"""Scenario 1: scanning a sandboxed IDE profile (T13)."""

import os
import unittest

from tests.e2e import support

GREEN_DIRS = ("Cache", "Code Cache", "GPUCache", "CachedData", "logs")


class ScanSandbox(support.SandboxTestCase):
    def test_scan_covers_the_profile_and_stays_inside_the_sandbox(self):
        sandbox = self.sandbox
        service = self.service_for()
        snapshot = support.run_scan(self, service)
        self.log("scan done", items=len(snapshot.get("items", [])))

        paths = {os.path.normcase(item["source_path"]) for item in snapshot["items"]}
        for name in GREEN_DIRS:
            wanted = os.path.normcase(os.path.join(sandbox.tool_root, name))
            self.assertIn(wanted, paths, "scan missed %s" % name)

        # The real profile must never be touched: every reported path lives in
        # the sandbox and none of them starts with the host %APPDATA%.
        real = support.real_appdata()
        for item in snapshot["items"]:
            support.assert_inside_sandbox(sandbox, item["source_path"], "scan item")
            if real:
                self.assertFalse(
                    os.path.normcase(item["source_path"]).startswith(os.path.normcase(real)),
                    "scan read the real profile: %s" % item["source_path"],
                )

        # The migrated directory matches the fixture byte for byte.
        cache = support.find_item(snapshot, sandbox.cache_path)
        expected = support.tree_totals(support.tree_state(sandbox.cache_path))
        self.assertEqual(cache["kind"], "dir")
        self.assertEqual(int(cache["bytes"]), expected["bytes"])
        self.assertEqual(int(cache["file_count"]), expected["files"])
        if sandbox.scale == "real":
            # Release hardening: the fixture must really be multi-GB, not a
            # handful of small files that make every stage look instant.
            self.assertGreaterEqual(
                expected["bytes"], 2 * 1024 ** 3, "the real-scale fixture is too small"
            )
        self.assertEqual(cache["safety"], "green", "Cache must be migratable")
        self.assertTrue(cache["can_migrate"])
        self.assertEqual(cache["access_errors"], 0)
        self.assertGreaterEqual(support.tree_depth(sandbox.cache_path), 4)

        stats = snapshot.get("stats") or {}
        self.assertGreater(int(stats.get("bytes") or 0), 0)
        self.assertGreater(int(stats.get("files") or 0), 0)
        self.assertGreater(int(stats.get("migratable_bytes") or 0), 0)
        self.assertEqual(int(stats.get("access_errors") or 0), 0)
        self.log("scan totals", stats=stats)

    def test_scan_twice_is_stable(self):
        service = self.service_for()
        first = support.run_scan(self, service)
        second = support.run_scan(self, service)
        first_pairs = sorted(
            (item["item_id"], int(item["bytes"]), int(item["file_count"]))
            for item in first["items"]
        )
        second_pairs = sorted(
            (item["item_id"], int(item["bytes"]), int(item["file_count"]))
            for item in second["items"]
        )
        self.assertEqual(first_pairs, second_pairs, "scan is not repeatable")

    def test_scan_without_any_ide_installed(self):
        """An empty profile reports zero items instead of failing."""
        sandbox = self.sandbox
        empty_profile = os.path.join(sandbox.root, "empty-profile")
        os.makedirs(empty_profile, exist_ok=True)
        environ = sandbox.environ()
        environ["APPDATA"] = empty_profile
        environ["LOCALAPPDATA"] = empty_profile
        service = self.service_for(environ=environ)
        snapshot = support.run_scan(self, service)
        self.assertEqual(snapshot.get("items"), [], "unexpected items: %r" % (snapshot["items"][:3],))


if __name__ == "__main__":
    unittest.main()
