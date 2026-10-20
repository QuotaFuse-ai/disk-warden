# -*- coding: utf-8 -*-
"""``AppService.reveal``: bridge-side "open folder" action."""

import os
import tempfile
import unittest
from unittest import mock

from src.app.errors import BridgeError
from tests.app_fakes import make_service


class RevealServiceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = self.temporary.name
        self.service = make_service(self)

    def test_reveal_opens_an_existing_directory(self):
        with mock.patch("src.platform.reveal._open_directory") as opener:
            result = self.service.reveal(self.root)
        absolute = os.path.abspath(self.root)
        self.assertEqual(result, {"opened": True, "path": absolute})
        opener.assert_called_once_with(absolute)

    def test_reveal_expands_environment_variables(self):
        with mock.patch.dict(os.environ, {"DW_TEST_DIR": self.root}):
            with mock.patch(
                "src.platform.reveal._open_directory"
            ) as opener:
                result = self.service.reveal("%DW_TEST_DIR%")
        absolute = os.path.abspath(self.root)
        self.assertEqual(result["path"], absolute)
        opener.assert_called_once_with(absolute)

    def test_missing_directory_is_not_found(self):
        missing = os.path.join(self.root, "does-not-exist")
        with self.assertRaises(BridgeError) as caught:
            self.service.reveal(missing)
        self.assertEqual(caught.exception.code, "NOT_FOUND")
        self.assertEqual(caught.exception.detail["path"], os.path.abspath(missing))

    def test_shell_access_denied_maps_to_permission_denied(self):
        error = OSError(5, "denied")
        error.winerror = 5
        with mock.patch(
            "src.platform.reveal._open_directory", side_effect=error
        ):
            with self.assertRaises(BridgeError) as caught:
                self.service.reveal(self.root)
        self.assertEqual(caught.exception.code, "PERMISSION_DENIED")

    def test_other_shell_errors_map_to_io_error(self):
        with mock.patch(
            "src.platform.reveal._open_directory",
            side_effect=OSError(31, "cannot start"),
        ):
            with self.assertRaises(BridgeError) as caught:
                self.service.reveal(self.root)
        self.assertEqual(caught.exception.code, "IO_ERROR")


if __name__ == "__main__":
    unittest.main()
