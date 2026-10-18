# -*- coding: utf-8 -*-
"""Unit tests for the T08 process-occupancy layer."""

import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.scan import (  # noqa: E402
    STATUS_NO_PROCESS_NAMES,
    STATUS_NOT_RUNNING,
    STATUS_RUNNING,
    STATUS_UNKNOWN,
    ProcessHit,
    blocked_tool_ids,
    detect_processes,
    load_catalog,
    summarize_processes,
)
from src.core.scan.catalog import parse_catalog  # noqa: E402
from src.platform import ProcessEnumerationError, ProcessInfo  # noqa: E402

REAL_CATALOG = REPO_ROOT / "data" / "ide-catalog.json"


def make_catalog(processes, tool_id="demo"):
    return parse_catalog(
        {
            "schema_version": 1,
            "source": "tests",
            "platform": "windows",
            "updated_at": "2026-10-19",
            "ides": [
                {
                    "id": tool_id,
                    "name": "Demo",
                    "kind": "ide",
                    "platform": "windows",
                    "processes": processes,
                    "directories": [
                        {
                            "path": "%APPDATA%\\Demo\\Cache\\",
                            "type": "dir",
                            "safety": "green",
                            "reason": "test",
                        }
                    ],
                }
            ],
        }
    )


def provider_with(*processes):
    return lambda: list(processes)


class MatchingTest(unittest.TestCase):
    def test_running_matches_case_insensitively_and_ignores_exe_suffix(self):
        catalog = make_catalog(["Demo.exe"])

        hits = detect_processes(
            catalog,
            provider_with(
                ProcessInfo(
                    pid=42,
                    name="demo.EXE",
                    exe="C:\\Program Files\\Demo\\demo.exe",
                )
            ),
        )

        hit = hits["demo"]
        self.assertEqual(STATUS_RUNNING, hit.status)
        self.assertTrue(hit.blocked_by_process)
        self.assertEqual([42], hit.pids)
        self.assertEqual(("Demo.exe",), hit.catalog_names)
        self.assertEqual(1, len(hit.matched))

    def test_matches_by_executable_path_when_name_is_empty(self):
        catalog = make_catalog(["demo"])

        hits = detect_processes(
            catalog,
            provider_with(ProcessInfo(pid=7, name="", exe="/opt/demo/bin/demo")),
        )

        self.assertEqual(STATUS_RUNNING, hits["demo"].status)

    def test_matches_macos_process_without_exe_suffix(self):
        catalog = make_catalog(["Cursor.exe"])

        hits = detect_processes(
            catalog, provider_with(ProcessInfo(pid=9, name="Cursor"))
        )

        self.assertEqual(STATUS_RUNNING, hits["demo"].status)

    def test_multiple_instances_are_all_reported(self):
        catalog = make_catalog(["demo.exe"])

        hits = detect_processes(
            catalog,
            provider_with(
                ProcessInfo(pid=1, name="demo.exe"),
                ProcessInfo(pid=2, name="demo.exe"),
                ProcessInfo(pid=3, name="other.exe"),
            ),
        )

        self.assertEqual([1, 2], hits["demo"].pids)

    def test_unrelated_process_means_not_running(self):
        catalog = make_catalog(["Demo.exe"])

        hits = detect_processes(
            catalog, provider_with(ProcessInfo(pid=5, name="other.exe"))
        )

        hit = hits["demo"]
        self.assertEqual(STATUS_NOT_RUNNING, hit.status)
        self.assertFalse(hit.blocked_by_process)
        self.assertEqual([], hit.pids)

    def test_tool_without_catalog_process_names_is_not_blocked(self):
        catalog = make_catalog([])

        hits = detect_processes(
            catalog, provider_with(ProcessInfo(pid=5, name="demo.exe"))
        )

        hit = hits["demo"]
        self.assertEqual(STATUS_NO_PROCESS_NAMES, hit.status)
        self.assertFalse(hit.blocked_by_process)


class EnumerationFailureTest(unittest.TestCase):
    def test_failure_is_unknown_and_blocks(self):
        catalog = make_catalog(["Demo.exe"])

        def failing():
            raise ProcessEnumerationError("tasklist unavailable")

        hits = detect_processes(catalog, failing)

        hit = hits["demo"]
        self.assertEqual(STATUS_UNKNOWN, hit.status)
        self.assertTrue(hit.blocked_by_process)
        self.assertIn("tasklist unavailable", hit.error)
        self.assertEqual(["demo"], blocked_tool_ids(hits))

    def test_unexpected_provider_error_is_also_unknown(self):
        catalog = make_catalog(["Demo.exe"])

        def failing():
            raise RuntimeError("boom")

        hits = detect_processes(catalog, failing)

        self.assertEqual(STATUS_UNKNOWN, hits["demo"].status)
        self.assertTrue(hits["demo"].blocked_by_process)


class CatalogDrivenTest(unittest.TestCase):
    def test_names_come_from_the_real_catalog(self):
        catalog = load_catalog(REAL_CATALOG)
        jetbrains = next(
            tool for tool in catalog.tools if tool.id == "jetbrains"
        )

        hits = detect_processes(
            catalog, provider_with(ProcessInfo(pid=1234, name="idea64.exe"))
        )

        self.assertEqual(STATUS_RUNNING, hits["jetbrains"].status)
        self.assertEqual(tuple(jetbrains.processes), hits["jetbrains"].catalog_names)
        self.assertEqual(STATUS_NOT_RUNNING, hits["cursor"].status)

    def test_every_catalog_tool_is_reported(self):
        catalog = load_catalog(REAL_CATALOG)

        hits = detect_processes(catalog, provider_with())

        self.assertEqual(
            set(tool.id for tool in catalog.tools), set(hits)
        )
        summary = summarize_processes(hits)
        self.assertEqual(len(catalog.tools), summary["tools"])
        self.assertEqual(0, summary["blocked"])

    def test_catalog_processes_are_not_hard_coded(self):
        # An empty catalog list must not accidentally match a running IDE.
        catalog = make_catalog([])
        hits = detect_processes(
            catalog, provider_with(ProcessInfo(pid=1, name="cursor.exe"))
        )
        self.assertEqual(STATUS_NO_PROCESS_NAMES, hits["demo"].status)


class SummarizeTest(unittest.TestCase):
    def test_summary_counts_statuses_and_blocked_tools(self):
        hits = {
            "a": ProcessHit(
                tool_id="a",
                status=STATUS_RUNNING,
                catalog_names=("demo.exe",),
                matched=(ProcessInfo(pid=1, name="demo.exe"),),
            ),
            "b": ProcessHit(
                tool_id="b",
                status=STATUS_UNKNOWN,
                catalog_names=("demo.exe",),
                error="denied",
            ),
            "c": ProcessHit(
                tool_id="c",
                status=STATUS_NOT_RUNNING,
                catalog_names=("demo.exe",),
            ),
        }

        summary = summarize_processes(hits)
        self.assertEqual(3, summary["tools"])
        self.assertEqual(1, summary[STATUS_RUNNING])
        self.assertEqual(1, summary[STATUS_NOT_RUNNING])
        self.assertEqual(1, summary[STATUS_UNKNOWN])
        self.assertEqual(2, summary["blocked"])
        self.assertEqual(["a", "b"], blocked_tool_ids(hits))


class PlatformEnumerationTest(unittest.TestCase):
    def test_current_process_is_visible(self):
        from src.platform import enumerate_processes

        try:
            processes = enumerate_processes()
        except ProcessEnumerationError as exc:
            self.skipTest("process enumeration unavailable: %s" % exc)

        pids = {process.pid for process in processes}
        self.assertIn(os.getpid(), pids)
        self.assertTrue(all(process.name for process in processes))


if __name__ == "__main__":
    unittest.main()
