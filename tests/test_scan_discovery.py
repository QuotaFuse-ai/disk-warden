# -*- coding: utf-8 -*-
"""Unit tests for the T06 catalog-driven scan layer."""

import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.scan import (  # noqa: E402
    CatalogError,
    STATUS_ABSENT,
    STATUS_NO_PERMISSION,
    STATUS_PRESENT,
    discover,
    load_catalog,
    parse_catalog,
)

REAL_CATALOG = REPO_ROOT / "data" / "ide-catalog.json"


def make_catalog(directories, tool_id="demo", name="Demo"):
    """Build a one-tool catalog from (path, type, safety) tuples."""
    return parse_catalog(
        {
            "schema_version": 1,
            "source": "tests",
            "platform": "windows",
            "updated_at": "2026-10-19",
            "ides": [
                {
                    "id": tool_id,
                    "name": name,
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


class FakeHome(object):
    """Temporary stand-in for %APPDATA% / %LOCALAPPDATA% / %USERPROFILE%."""

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

    def mkdir(self, base, relative):
        path = Path(base) / relative
        path.mkdir(parents=True, exist_ok=True)
        return path

    def touch(self, base, relative):
        path = Path(base) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        return path


def tool_by_id(result, tool_id):
    for tool in result.tools:
        if tool.id == tool_id:
            return tool
    raise AssertionError("tool %r not found in result" % tool_id)


def hit_by_template(tool, template):
    for hit in tool.directories:
        if hit.template == template:
            return hit
    raise AssertionError("template %r not found in tool %r" % (template, tool.id))


def snapshot_tree(root):
    items = []
    for dirpath, dirnames, filenames in os.walk(str(root)):
        for name in list(dirnames) + list(filenames):
            path = Path(dirpath) / name
            info = path.lstat()
            items.append(
                (
                    str(path.relative_to(root)),
                    info.st_size,
                    info.st_mtime_ns,
                    os.path.isdir(str(path)),
                )
            )
    items.sort()
    return items


class CursorDiscoveryTest(unittest.TestCase):
    def test_cursor_entries_are_found_by_real_catalog(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            home = FakeHome(tmp)
            home.mkdir(home.appdata, "Cursor/Cache")
            home.mkdir(home.appdata, "Cursor/User/workspaceStorage")
            home.touch(home.appdata, "Cursor/User/workspaceStorage/aaaa.pack")
            home.touch(home.appdata, "Cursor/User/workspaceStorage/notes.txt")
            home.touch(home.appdata, "Cursor/Login Data")
            home.touch(home.appdata, "Cursor/User/settings.json")

            result = discover(load_catalog(REAL_CATALOG), home.environ)
            cursor = tool_by_id(result, "cursor")

            self.assertEqual(STATUS_PRESENT, cursor.status)
            self.assertEqual(
                STATUS_PRESENT,
                hit_by_template(
                    cursor, "%APPDATA%\\Cursor\\Cache\\"
                ).status,
            )
            self.assertEqual(
                STATUS_ABSENT,
                hit_by_template(
                    cursor, "%APPDATA%\\Cursor\\CachedData\\"
                ).status,
            )

            glob_hit = hit_by_template(
                cursor, "%APPDATA%\\Cursor\\User\\workspaceStorage\\*.pack"
            )
            self.assertEqual(STATUS_PRESENT, glob_hit.status)
            self.assertEqual(
                ["aaaa.pack"],
                [Path(path).name for path in glob_hit.resolved],
            )

            login = hit_by_template(cursor, "%APPDATA%\\Cursor\\Login Data")
            self.assertEqual(STATUS_PRESENT, login.status)
            self.assertEqual("red", login.safety)


class TraeCnDiscoveryTest(unittest.TestCase):
    def test_old_trae_cn_root_is_recognised(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            home = FakeHome(tmp)
            home.mkdir(home.appdata, "Trae CN/Cache")

            result = discover(load_catalog(REAL_CATALOG), home.environ)
            trae_cn = tool_by_id(result, "trae-cn")

            old_hit = hit_by_template(
                trae_cn, "%APPDATA%\\Trae CN\\Cache\\"
            )
            new_hit = hit_by_template(
                trae_cn, "%APPDATA%\\TRAE SOLO CN\\Cache\\"
            )

            self.assertEqual(STATUS_PRESENT, old_hit.status)
            self.assertEqual(STATUS_ABSENT, new_hit.status)
            self.assertEqual(STATUS_PRESENT, trae_cn.status)

    def test_renamed_trae_solo_cn_root_is_recognised(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            home = FakeHome(tmp)
            home.mkdir(home.appdata, "TRAE SOLO CN/Cache")

            result = discover(load_catalog(REAL_CATALOG), home.environ)
            trae_cn = tool_by_id(result, "trae-cn")

            self.assertEqual(
                STATUS_PRESENT,
                hit_by_template(
                    trae_cn, "%APPDATA%\\TRAE SOLO CN\\Cache\\"
                ).status,
            )
            self.assertEqual(
                STATUS_ABSENT,
                hit_by_template(trae_cn, "%APPDATA%\\Trae CN\\Cache\\").status,
            )

    def test_both_generations_coexist(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            home = FakeHome(tmp)
            home.mkdir(home.appdata, "Trae CN/Cache")
            home.mkdir(home.appdata, "TRAE SOLO CN/Cache")

            result = discover(load_catalog(REAL_CATALOG), home.environ)
            trae_cn = tool_by_id(result, "trae-cn")

            self.assertEqual(
                STATUS_PRESENT,
                hit_by_template(trae_cn, "%APPDATA%\\Trae CN\\Cache\\").status,
            )
            self.assertEqual(
                STATUS_PRESENT,
                hit_by_template(
                    trae_cn, "%APPDATA%\\TRAE SOLO CN\\Cache\\"
                ).status,
            )


class ClaudeCodeDiscoveryTest(unittest.TestCase):
    def test_project_and_session_placeholders_expand(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            home = FakeHome(tmp)
            home.mkdir(home.home, ".claude/debug")
            project = home.mkdir(home.home, ".claude/projects/项目-甲")
            home.mkdir(home.home, ".claude/projects/项目-甲/session-1/tool-results")
            home.mkdir(home.home, ".claude/projects/项目-甲/memory")
            # A file at the wildcard level must not be treated as a project dir.
            home.touch(home.home, ".claude/projects/not-a-project.txt")

            result = discover(load_catalog(REAL_CATALOG), home.environ)
            claude = tool_by_id(result, "claude-code")

            self.assertEqual(STATUS_PRESENT, claude.status)
            self.assertEqual(
                STATUS_PRESENT,
                hit_by_template(claude, "%USERPROFILE%\\.claude\\debug\\").status,
            )

            projects = hit_by_template(
                claude, "%USERPROFILE%\\.claude\\projects\\<项目>\\"
            )
            self.assertEqual(STATUS_PRESENT, projects.status)
            self.assertEqual(
                [os.path.normcase(str(project))],
                [os.path.normcase(path) for path in projects.resolved],
            )

            tool_results = hit_by_template(
                claude,
                "%USERPROFILE%\\.claude\\projects\\<项目>\\<session>\\tool-results\\",
            )
            self.assertEqual(STATUS_PRESENT, tool_results.status)
            self.assertEqual(1, len(tool_results.resolved))

            memory = hit_by_template(
                claude, "%USERPROFILE%\\.claude\\projects\\<项目>\\memory\\"
            )
            self.assertEqual(STATUS_PRESENT, memory.status)
            self.assertEqual("red", memory.safety)


class EnvironmentExpansionTest(unittest.TestCase):
    def test_variable_lookup_is_case_insensitive(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            home = FakeHome(tmp)
            home.mkdir(home.appdata, "Cursor/Cache")
            catalog = make_catalog(
                [("%appdata%\\Cursor\\Cache\\", "dir", "green")]
            )

            result = discover(catalog, home.environ)
            hit = result.tools[0].directories[0]

            self.assertEqual(STATUS_PRESENT, hit.status)
            self.assertEqual(home.appdata, Path(hit.resolved[0]).parent.parent)

    def test_unknown_variable_is_reported_without_crashing(self):
        catalog = make_catalog(
            [("%APPDATA%\\Cursor\\Cache\\", "dir", "green")]
        )

        result = discover(catalog, {"USERPROFILE": "C:\\Users\\tester"})
        hit = result.tools[0].directories[0]

        self.assertEqual(STATUS_ABSENT, hit.status)
        self.assertEqual("unresolved_environment", hit.detail)
        self.assertEqual(1, len(result.warnings))
        self.assertEqual("unresolved_environment", result.warnings[0].code)
        self.assertEqual("demo", result.warnings[0].tool_id)


class ResilienceTest(unittest.TestCase):
    def test_missing_paths_do_not_abort_the_scan(self):
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            home = FakeHome(tmp)
            home.mkdir(home.appdata, "Demo/Present")
            catalog = make_catalog(
                [
                    ("%APPDATA%\\Demo\\Present\\", "dir", "green"),
                    ("%APPDATA%\\Demo\\Missing\\", "dir", "green"),
                    ("%APPDATA%\\Demo\\Denied\\", "dir", "green"),
                ]
            )

            original_stat = os.stat

            def fake_stat(path, *args, **kwargs):
                if str(path).endswith("Denied"):
                    raise PermissionError("denied by test")
                return original_stat(path, *args, **kwargs)

            with mock.patch("os.stat", side_effect=fake_stat):
                result = discover(catalog, home.environ)

            hits = result.tools[0].directories
            self.assertEqual(3, len(hits))
            self.assertEqual(STATUS_PRESENT, hits[0].status)
            self.assertEqual(STATUS_ABSENT, hits[1].status)
            self.assertEqual(STATUS_NO_PERMISSION, hits[2].status)
            self.assertEqual(STATUS_PRESENT, result.tools[0].status)

    def test_permission_denied_placeholder_listing(self):
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            home = FakeHome(tmp)
            catalog = make_catalog(
                [("%USERPROFILE%\\.claude\\projects\\<项目>\\", "dir", "green")]
            )

            with mock.patch("os.scandir", side_effect=PermissionError("nope")):
                result = discover(catalog, home.environ)

            hit = result.tools[0].directories[0]
            self.assertEqual(STATUS_NO_PERMISSION, hit.status)
            self.assertEqual(STATUS_NO_PERMISSION, result.tools[0].status)
            self.assertEqual(1, len(result.warnings))
            self.assertEqual("permission_denied", result.warnings[0].code)

    def test_directory_entry_pointing_at_a_file_is_absent(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            home = FakeHome(tmp)
            home.touch(home.appdata, "Demo/Cache")
            catalog = make_catalog(
                [("%APPDATA%\\Demo\\Cache\\", "dir", "green")]
            )

            result = discover(catalog, home.environ)
            hit = result.tools[0].directories[0]

            self.assertEqual(STATUS_ABSENT, hit.status)
            self.assertEqual("type_mismatch", hit.detail)

    def test_unrelated_os_error_is_reported_not_raised(self):
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            home = FakeHome(tmp)
            home.mkdir(home.appdata, "Demo/Cache")
            catalog = make_catalog(
                [("%APPDATA%\\Demo\\Cache\\", "dir", "green")]
            )

            with mock.patch(
                "os.stat", side_effect=OSError(123, "invalid name")
            ):
                result = discover(catalog, home.environ)

            hit = result.tools[0].directories[0]
            self.assertEqual(STATUS_NO_PERMISSION, hit.status)
            self.assertEqual(1, len(result.warnings))


class ReadOnlyAndCoverageTest(unittest.TestCase):
    def test_scan_does_not_modify_the_scanned_tree(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            home = FakeHome(tmp)
            home.mkdir(home.appdata, "Cursor/Cache")
            home.mkdir(home.appdata, "Cursor/User/workspaceStorage")
            home.touch(home.appdata, "Cursor/User/workspaceStorage/a.pack")
            home.touch(home.appdata, "Cursor/Login Data")
            home.mkdir(home.appdata, "Trae CN/Cache")
            home.mkdir(home.home, ".claude/debug")
            home.mkdir(home.home, ".claude/projects/p1/s1/tool-results")

            before = snapshot_tree(home.root)
            discover(load_catalog(REAL_CATALOG), home.environ)
            after = snapshot_tree(home.root)

            self.assertEqual(before, after)

    def test_every_catalog_tool_is_reported(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            home = FakeHome(tmp)
            catalog = load_catalog(REAL_CATALOG)
            result = discover(catalog, home.environ)

            self.assertEqual(
                set(tool.id for tool in catalog.tools),
                set(tool.id for tool in result.tools),
            )
            self.assertEqual(30, len(result.tools))

            summary = result.summary()
            self.assertEqual(30, summary["tools"]["total"])
            self.assertEqual(162, summary["directories"]["total"])
            for status in (STATUS_PRESENT, STATUS_ABSENT, STATUS_NO_PERMISSION):
                self.assertIn(status, summary["tools"])
                self.assertIn(status, summary["directories"])
            self.assertEqual(
                summary["tools"]["total"],
                summary["tools"][STATUS_PRESENT]
                + summary["tools"][STATUS_ABSENT]
                + summary["tools"][STATUS_NO_PERMISSION],
            )

    def test_tool_level_status_prefers_present(self):
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            home = FakeHome(tmp)
            home.mkdir(home.appdata, "Demo/Present")
            catalog = make_catalog(
                [
                    ("%APPDATA%\\Demo\\Present\\", "dir", "green"),
                    ("%APPDATA%\\Demo\\Denied\\", "dir", "green"),
                ]
            )

            original_stat = os.stat

            def fake_stat(path, *args, **kwargs):
                if str(path).endswith("Denied"):
                    raise PermissionError("denied by test")
                return original_stat(path, *args, **kwargs)

            with mock.patch("os.stat", side_effect=fake_stat):
                result = discover(catalog, home.environ)

            self.assertEqual(STATUS_PRESENT, result.tools[0].status)


class CatalogLoaderTest(unittest.TestCase):
    def test_duplicate_tool_ids_are_rejected(self):
        raw = {
            "schema_version": 1,
            "source": "tests",
            "platform": "windows",
            "updated_at": "2026-10-19",
            "ides": [
                {
                    "id": "demo",
                    "name": "Demo",
                    "kind": "ide",
                    "platform": "windows",
                    "processes": [],
                    "directories": [
                        {
                            "path": "%APPDATA%\\Demo\\",
                            "type": "dir",
                            "safety": "green",
                            "reason": "test",
                        }
                    ],
                },
                {
                    "id": "demo",
                    "name": "Demo 2",
                    "kind": "ide",
                    "platform": "windows",
                    "processes": [],
                    "directories": [
                        {
                            "path": "%APPDATA%\\Demo2\\",
                            "type": "dir",
                            "safety": "green",
                            "reason": "test",
                        }
                    ],
                },
            ],
        }

        with self.assertRaises(CatalogError):
            parse_catalog(raw)

    def test_invalid_safety_value_is_rejected(self):
        raw = {
            "schema_version": 1,
            "source": "tests",
            "platform": "windows",
            "updated_at": "2026-10-19",
            "ides": [
                {
                    "id": "demo",
                    "name": "Demo",
                    "kind": "ide",
                    "platform": "windows",
                    "processes": [],
                    "directories": [
                        {
                            "path": "%APPDATA%\\Demo\\",
                            "type": "dir",
                            "safety": "purple",
                            "reason": "test",
                        }
                    ],
                }
            ],
        }

        with self.assertRaises(CatalogError):
            parse_catalog(raw)


if __name__ == "__main__":
    unittest.main()
