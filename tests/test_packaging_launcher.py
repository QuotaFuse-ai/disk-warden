# -*- coding: utf-8 -*-
"""Launcher refuses to start on a stale Python runtime left by an old package."""

import importlib.util
import os
import sys
import tempfile
import unittest
from unittest import mock

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "packaging",
    "launcher.py",
)
_SPEC = importlib.util.spec_from_file_location("diskw_launcher", _MODULE_PATH)
launcher = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(launcher)


class RuntimeDllNameTest(unittest.TestCase):
    def test_versioned_dlls_are_runtime_dlls(self):
        for name in ("python38.dll", "python312.dll", "Python311.DLL"):
            self.assertTrue(launcher._is_python_runtime_dll(name), name)

    def test_generic_and_unrelated_dlls_are_ignored(self):
        for name in ("python3.dll", "python3.dll.bak", "python.dll", "python38.dll.old", "VCRUNTIME140.dll"):
            self.assertFalse(launcher._is_python_runtime_dll(name), name)


class StalePythonDllTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app_dir = self.tmp.name

    def _touch(self, name):
        with open(os.path.join(self.app_dir, name), "wb"):
            pass

    def test_reports_dlls_this_build_did_not_ship(self):
        self._touch("python38.dll")
        self._touch("python312.dll")
        self.assertEqual(launcher._stale_python_dlls(self.app_dir, "python312.dll"), ["python38.dll"])

    def test_clean_layout_has_no_stale_dlls(self):
        self._touch("python312.dll")
        self._touch("python3.dll")
        self.assertEqual(launcher._stale_python_dlls(self.app_dir, "python312.dll"), [])

    def test_missing_dir_is_not_an_error(self):
        self.assertEqual(launcher._stale_python_dlls(os.path.join(self.app_dir, "nope"), ""), [])

    def test_directories_are_ignored(self):
        os.makedirs(os.path.join(self.app_dir, "python38.dll"))
        self.assertEqual(launcher._stale_python_dlls(self.app_dir, ""), [])


class CheckRuntimeLayoutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _frozen(self, dlls, bundled="python312.dll", message=None):
        for name in dlls:
            with open(os.path.join(self.tmp.name, name), "wb"):
                pass
        message = message if message is not None else mock.Mock()
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", os.path.join(self.tmp.name, "DiskWarden.exe")), \
                mock.patch.object(launcher, "_bundled_python_dll", return_value=bundled), \
                mock.patch.object(launcher, "_message", message):
            result = launcher._check_runtime_layout()
        return result, message

    def test_source_tree_is_always_allowed(self):
        self.assertTrue(launcher._check_runtime_layout())

    def test_frozen_layout_without_leftovers_starts(self):
        result, message = self._frozen(["python312.dll"])
        self.assertTrue(result)
        message.assert_not_called()

    def test_leftover_python38_dll_blocks_start_with_clear_message(self):
        result, message = self._frozen(["python312.dll", "python38.dll"])
        self.assertFalse(result)
        text = message.call_args[0][0]
        self.assertIn("python38.dll", text)
        self.assertIn("全新的空目录", text)


if __name__ == "__main__":
    unittest.main()
