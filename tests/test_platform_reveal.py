# -*- coding: utf-8 -*-
"""Directory reveal helpers (UI "open folder" buttons).

``_open_directory`` is patched so the tests never start a real Explorer
window; the shell decision itself is three lines of platform dispatch.
"""

import os
import tempfile
import unittest
from unittest import mock

from src.platform.reveal import expand_path, reveal_directory


class ExpandPathTest(unittest.TestCase):
    def test_percent_variables_are_expanded(self):
        with mock.patch.dict(os.environ, {"DW_TEST_ROOT": r"C:\Temp\dw"}):
            self.assertEqual(
                expand_path(r"%DW_TEST_ROOT%\cache"),
                os.path.abspath(r"C:\Temp\dw\cache"),
            )

    def test_unknown_variables_are_left_alone(self):
        self.assertEqual(
            expand_path(r"%DW_TEST_UNKNOWN%\cache"),
            os.path.abspath(r"%DW_TEST_UNKNOWN%\cache"),
        )

    def test_tilde_is_expanded(self):
        self.assertEqual(
            expand_path("~"),
            os.path.abspath(os.path.expanduser("~")),
        )


class RevealDirectoryTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = temporary.name

    def test_existing_directory_is_opened(self):
        with mock.patch(
            "src.platform.reveal._open_directory"
        ) as opener:
            opened = reveal_directory(self.root)
        self.assertEqual(opened, os.path.abspath(self.root))
        opener.assert_called_once_with(os.path.abspath(self.root))

    def test_missing_directory_is_rejected_before_the_shell(self):
        missing = os.path.join(self.root, "does-not-exist")
        with mock.patch("src.platform.reveal._open_directory") as opener:
            with self.assertRaises(FileNotFoundError):
                reveal_directory(missing)
        opener.assert_not_called()

    def test_shell_failure_propagates(self):
        with mock.patch(
            "src.platform.reveal._open_directory",
            side_effect=OSError(5, "denied"),
        ):
            with self.assertRaises(OSError):
                reveal_directory(self.root)


if __name__ == "__main__":
    unittest.main()
