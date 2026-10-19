# -*- coding: utf-8 -*-
"""Unit tests for the T08 size-measurement layer."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.scan import (  # noqa: E402
    CancelToken,
    ScanCancelled,
    make_item_id,
    targets_from_discovery,
)
from src.core.scan.cancel import is_cancelled  # noqa: E402
from src.core.scan.catalog import parse_catalog  # noqa: E402
from src.core.scan.discovery import discover  # noqa: E402
from src.core.scan.size import (  # noqa: E402
    WARNING_MISSING,
    WARNING_PERMISSION_DENIED,
    WARNING_REPARSE_SKIPPED,
    MeasureTarget,
    measure,
)


def write_file(path, size, mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    if mtime is not None:
        os.utime(str(path), (mtime, mtime))
    return path


def make_dir_link(link, target):
    """Create a directory junction (Windows) or symlink (POSIX)."""
    link = str(link)
    target = str(target)
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", link, target],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            raise unittest.SkipTest(
                "cannot create junction: %r" % (result.stderr,)
            )
        return
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        raise unittest.SkipTest("cannot create symlink: %s" % exc)


def remove_dir_link(link):
    if os.name == "nt":
        os.rmdir(str(link))
    else:
        os.unlink(str(link))


class MeasureTotalsTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.root), True)

    def test_recursive_totals_and_file_count(self):
        write_file(self.root / "a.bin", 100, mtime=1600000000)
        write_file(self.root / "sub" / "b.bin", 50, mtime=1600000500)
        write_file(self.root / "sub" / "deep" / "c.bin", 25)

        report = measure([MeasureTarget("demo", str(self.root), "Cache")])

        self.assertEqual(1, len(report))
        item = report.items[0]
        self.assertEqual("dir", item.kind)
        self.assertEqual(175, item.bytes)
        self.assertEqual(3, item.file_count)
        self.assertEqual(0, item.access_errors)
        self.assertFalse(item.is_reparse)
        self.assertEqual("Cache", item.rel_path)
        self.assertEqual(make_item_id("demo", "Cache"), item.item_id)
        self.assertEqual(175, report.summary()["bytes"])
        parsed = datetime.fromisoformat(item.mtime_max)
        self.assertGreaterEqual(parsed.timestamp(), 1600000500)

    def test_totals_match_a_plain_recursive_walk(self):
        sizes = [0, 1, 4095, 4096, 123456]
        for index, size in enumerate(sizes):
            write_file(self.root / ("d%d" % index) / "f.bin", size)

        expected_bytes = 0
        expected_files = 0
        for dirpath, _dirnames, filenames in os.walk(str(self.root)):
            for name in filenames:
                expected_bytes += os.stat(os.path.join(dirpath, name)).st_size
                expected_files += 1

        report = measure([MeasureTarget("demo", str(self.root), "Cache")])

        self.assertEqual(expected_bytes, report.items[0].bytes)
        self.assertEqual(expected_files, report.items[0].file_count)

    def test_file_target_counts_itself(self):
        target = write_file(self.root / "state.vscdb", 490)

        report = measure([MeasureTarget("demo", str(target), "User/state.vscdb")])

        item = report.items[0]
        self.assertEqual("file", item.kind)
        self.assertEqual(490, item.bytes)
        self.assertEqual(1, item.file_count)

    def test_missing_target_is_a_warning_not_a_crash(self):
        report = measure(
            [
                MeasureTarget("demo", str(self.root / "missing"), "missing"),
                MeasureTarget("demo", str(self.root), "Cache"),
            ]
        )

        self.assertEqual(2, len(report.items))
        missing = report.items[0]
        self.assertEqual(0, missing.bytes)
        self.assertEqual(1, missing.access_errors)
        self.assertEqual(WARNING_MISSING, report.warnings[0].code)
        self.assertEqual("demo", report.warnings[0].tool_id)

    def test_plain_path_string_is_accepted(self):
        write_file(self.root / "a.bin", 10)

        report = measure([str(self.root)])

        self.assertEqual(10, report.items[0].bytes)
        self.assertEqual(self.root.name, report.items[0].rel_path)


class ProgressTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.root), True)
        for index in range(5):
            write_file(self.root / ("f%d.bin" % index), 10)

    def test_final_event_always_reports_done_and_totals(self):
        events = []

        report = measure(
            [MeasureTarget("demo", str(self.root), "Cache")],
            progress_cb=events.append,
            throttle_seconds=10.0,
        )

        self.assertTrue(events)
        self.assertTrue(events[-1]["done"])
        self.assertEqual(1, events[-1]["targets_done"])
        self.assertEqual(1, events[-1]["targets_total"])
        self.assertEqual(report.summary()["bytes"], events[-1]["bytes_done"])
        self.assertEqual(5, events[-1]["files_done"])
        # Throttling keeps a fast scan from flooding the UI.
        self.assertLessEqual(len(events), 2)

    def test_zero_throttle_emits_per_entry_progress(self):
        events = []

        measure(
            [MeasureTarget("demo", str(self.root), "Cache")],
            progress_cb=events.append,
            throttle_seconds=0.0,
        )

        self.assertGreaterEqual(len(events), 6)

    def test_callback_can_cancel_scan(self):
        token = CancelToken()
        events = []

        def on_progress(event):
            events.append(event)
            token.cancel()

        with self.assertRaises(ScanCancelled):
            measure(
                [MeasureTarget("demo", str(self.root), "Cache")],
                cancel_token=token,
                progress_cb=on_progress,
                throttle_seconds=0.0,
            )
        self.assertTrue(is_cancelled(token))

    def test_threading_event_is_supported(self):
        import threading

        token = threading.Event()
        token.set()
        with self.assertRaises(ScanCancelled):
            measure([MeasureTarget("demo", str(self.root), "Cache")], token)

    def test_pre_cancelled_token_raises(self):
        token = CancelToken()
        token.cancel()
        with self.assertRaises(ScanCancelled):
            measure([str(self.root)], cancel_token=token)


class ErrorIsolationTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.root), True)

    def _scan_with_failing_path(self, failing_name, exception):
        original_scandir = os.scandir

        def fake_scandir(path):
            if str(path).endswith(failing_name):
                raise exception
            return original_scandir(path)

        with mock.patch(
            "src.core.scan.size.os.scandir", side_effect=fake_scandir
        ):
            return measure([MeasureTarget("demo", str(self.root), "Cache")])

    def test_permission_denied_subdir_does_not_abort_scan(self):
        write_file(self.root / "open" / "a.bin", 100)
        (self.root / "Denied").mkdir()

        report = self._scan_with_failing_path(
            "Denied", PermissionError("denied by test")
        )

        item = report.items[0]
        self.assertEqual(100, item.bytes)
        self.assertEqual(1, item.file_count)
        self.assertEqual(1, item.access_errors)
        self.assertEqual(WARNING_PERMISSION_DENIED, report.warnings[0].code)

    def test_vanished_subdir_does_not_abort_scan(self):
        write_file(self.root / "open" / "a.bin", 100)
        (self.root / "Gone").mkdir()

        report = self._scan_with_failing_path(
            "Gone", FileNotFoundError("gone")
        )

        item = report.items[0]
        self.assertEqual(100, item.bytes)
        self.assertEqual(1, item.access_errors)
        self.assertEqual(WARNING_MISSING, report.warnings[0].code)

    def test_one_bad_entry_does_not_hide_siblings(self):
        write_file(self.root / "a.bin", 10)
        write_file(self.root / "b.bin", 20)

        original_stat = os.stat

        # DirEntry.stat is implemented in C; stub the entry iterator instead.
        class StubEntry(object):
            def __init__(self, path):
                self.path = path
                self.name = os.path.basename(path)

            def stat(self, follow_symlinks=True):
                if self.path.endswith("b.bin"):
                    raise PermissionError("denied by test")
                return original_stat(self.path, follow_symlinks=follow_symlinks)

        class StubScandir(object):
            def __init__(self, entries):
                self._entries = entries

            def __enter__(self):
                return iter(self._entries)

            def __exit__(self, *exc_info):
                return False

        def fake_scandir(path):
            if str(path) == str(self.root):
                return StubScandir(
                    [
                        StubEntry(str(self.root / "a.bin")),
                        StubEntry(str(self.root / "b.bin")),
                    ]
                )
            return os.scandir(path)

        with mock.patch(
            "src.core.scan.size.os.scandir", side_effect=fake_scandir
        ):
            report = measure([MeasureTarget("demo", str(self.root), "Cache")])

        item = report.items[0]
        self.assertEqual(10, item.bytes)
        self.assertEqual(1, item.file_count)
        self.assertEqual(1, item.access_errors)
        self.assertEqual(WARNING_PERMISSION_DENIED, report.warnings[0].code)


class ReparsePointTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.outside = Path(tempfile.mkdtemp())
        self.links = []

        def cleanup():
            for link in self.links:
                try:
                    remove_dir_link(link)
                except OSError:
                    pass
            shutil.rmtree(str(self.root), True)
            shutil.rmtree(str(self.outside), True)

        self.addCleanup(cleanup)

    def _link(self, path, target):
        make_dir_link(path, target)
        self.links.append(path)
        return path

    def test_nested_junction_is_not_followed_or_counted(self):
        write_file(self.root / "real" / "a.bin", 10)
        write_file(self.outside / "secret.bin", 5000)
        self._link(self.root / "link", self.outside)

        report = measure([MeasureTarget("demo", str(self.root), "Cache")])

        item = report.items[0]
        self.assertEqual(10, item.bytes)
        self.assertEqual(1, item.file_count)
        self.assertFalse(item.is_reparse)
        codes = [warning.code for warning in report.warnings]
        self.assertIn(WARNING_REPARSE_SKIPPED, codes)

    def test_self_referencing_junction_terminates(self):
        write_file(self.root / "a.bin", 10)
        self._link(self.root / "loop", self.root)

        report = measure([MeasureTarget("demo", str(self.root), "Cache")])

        item = report.items[0]
        self.assertEqual(10, item.bytes)
        self.assertEqual(1, item.file_count)

    def test_target_itself_is_reparse(self):
        write_file(self.outside / "a.bin", 100)
        link = self._link(self.root / "link", self.outside)

        report = measure([MeasureTarget("demo", str(link), "Cache")])

        item = report.items[0]
        self.assertTrue(item.is_reparse)
        self.assertEqual(0, item.bytes)
        self.assertEqual(0, item.file_count)
        self.assertEqual(WARNING_REPARSE_SKIPPED, report.warnings[0].code)

    def test_symlinked_file_is_not_counted(self):
        write_file(self.outside / "secret.bin", 5000)
        write_file(self.root / "plain.bin", 10)
        link = self.root / "alias.bin"
        try:
            if os.name == "nt":
                result = subprocess.run(
                    ["cmd", "/c", "mklink", str(link),
                     str(self.outside / "secret.bin")],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                if result.returncode != 0:
                    raise unittest.SkipTest("cannot create file symlink")
            else:
                os.symlink(
                    str(self.outside / "secret.bin"), str(link)
                )
        except (OSError, NotImplementedError) as exc:
            raise unittest.SkipTest("cannot create file symlink: %s" % exc)

        report = measure([MeasureTarget("demo", str(self.root), "Cache")])

        item = report.items[0]
        self.assertEqual(10, item.bytes)
        self.assertEqual(1, item.file_count)


class FakeHome(object):
    def __init__(self, root):
        self.root = Path(root)
        self.appdata = self.root / "AppData" / "Roaming"
        self.localappdata = self.root / "AppData" / "Local"
        self.home = self.root / "Users" / "tester"
        for path in (self.appdata, self.localappdata, self.home):
            path.mkdir(parents=True, exist_ok=True)

    @property
    def environ(self):
        return {
            "APPDATA": str(self.appdata),
            "LOCALAPPDATA": str(self.localappdata),
            "USERPROFILE": str(self.home),
        }

    def write(self, base, relative, size):
        path = Path(base) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        return path


def make_catalog(directories, tool_id="demo"):
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
                    "processes": [],
                    "directories": [
                        {
                            "path": path,
                            "type": entry_type,
                            "safety": safety,
                            "reason": "test entry",
                        }
                        for path, entry_type, safety in directories
                    ],
                }
            ],
        }
    )


@unittest.skipUnless(os.name == "nt", "catalog paths are Windows-oriented")
class TargetsFromDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.home = FakeHome(self.tmp)

    def test_rel_paths_are_anchored_at_the_tool_root(self):
        self.home.write(self.home.appdata, "Demo/Cache/a.bin", 100)
        self.home.write(
            self.home.appdata, "Demo/User/workspaceStorage/aaaa.pack", 50
        )
        self.home.write(self.home.appdata, "Demo/Login Data", 10)
        catalog = make_catalog(
            [
                ("%APPDATA%\\Demo\\Cache\\", "dir", "green"),
                (
                    "%APPDATA%\\Demo\\User\\workspaceStorage\\*.pack",
                    "glob",
                    "green",
                ),
                ("%APPDATA%\\Demo\\Login Data", "file", "red"),
                ("%APPDATA%\\Demo\\Missing\\", "dir", "green"),
            ]
        )

        result = discover(catalog, self.home.environ)
        targets = targets_from_discovery(result)

        self.assertEqual(3, len(targets))
        self.assertEqual(
            ["Cache", "User/workspaceStorage/aaaa.pack", "Login Data"],
            [target.rel_path for target in targets],
        )

        report = measure(targets)
        self.assertEqual(160, report.summary()["bytes"])
        self.assertEqual(3, report.summary()["files"])
        self.assertEqual(
            len({item.item_id for item in report.items}),
            len(report.items),
        )

    def test_item_ids_are_stable_across_scans(self):
        self.home.write(self.home.appdata, "Demo/Cache/a.bin", 100)
        catalog = make_catalog(
            [("%APPDATA%\\Demo\\Cache\\", "dir", "green")]
        )

        first = targets_from_discovery(discover(catalog, self.home.environ))
        second = targets_from_discovery(discover(catalog, self.home.environ))

        self.assertEqual(
            [measure([target]).items[0].item_id for target in first],
            [measure([target]).items[0].item_id for target in second],
        )


if __name__ == "__main__":
    unittest.main()
