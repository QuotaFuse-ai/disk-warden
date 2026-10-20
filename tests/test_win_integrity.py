# -*- coding: utf-8 -*-
"""Windows mandatory-integrity diagnostics.

The queries themselves need a real Windows token and SACL, so these tests
cover the level arithmetic, the unlabelled-object default and the error text;
the live values are read but only constrained to the documented levels.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

if not sys.platform.startswith("win"):
    raise unittest.SkipTest("mandatory integrity levels exist on Windows only")

from unittest import mock  # noqa: E402

from src.platform.win import integrity  # noqa: E402


class LevelNameTests(unittest.TestCase):
    def test_documented_levels(self):
        self.assertEqual(integrity.level_name(integrity.UNTRUSTED), "Untrusted")
        self.assertEqual(integrity.level_name(integrity.LOW), "Low")
        self.assertEqual(integrity.level_name(integrity.MEDIUM), "Medium")
        self.assertEqual(integrity.level_name(integrity.HIGH), "High")
        self.assertEqual(integrity.level_name(integrity.SYSTEM), "System")

    def test_custom_level_is_rendered_as_hex(self):
        self.assertEqual(integrity.level_name(0x5000), "0x5000")

    def test_missing_level_is_unknown(self):
        self.assertEqual(integrity.level_name(None), "unknown")


class TokenTests(unittest.TestCase):
    def test_process_level_is_readable(self):
        level = integrity.process_integrity_level()
        self.assertIsInstance(level, int)
        self.assertGreaterEqual(level, integrity.UNTRUSTED)

    def test_process_level_never_raises(self):
        with mock.patch.object(integrity, "_open_token", return_value=None):
            self.assertIsNone(integrity.process_integrity_level())


class ObjectLevelTests(unittest.TestCase):
    def test_unlabelled_directory_defaults_to_medium(self):
        with tempfile.TemporaryDirectory(prefix="dw-integrity-") as folder:
            self.assertIsNone(integrity.explicit_object_level(folder))
            self.assertEqual(
                integrity.object_integrity_level(folder), integrity.MEDIUM
            )

    def test_missing_path_does_not_raise(self):
        missing = os.path.join(
            tempfile.gettempdir(), "dw-missing-integrity-probe"
        )
        self.assertEqual(
            integrity.object_integrity_level(missing), integrity.MEDIUM
        )
        self.assertEqual(integrity.describe_denial(missing), "")
        self.assertIsNone(integrity.integrity_denial(missing))


class DenialTextTests(unittest.TestCase):
    def test_text_names_both_levels_and_rules_out_a_handle(self):
        with mock.patch.object(
            integrity,
            "integrity_denial",
            return_value=(integrity.LOW, integrity.MEDIUM),
        ):
            text = integrity.describe_denial(
                os.path.join("C:\\", "Users", "someone", "AppData", "Trae")
            )

        self.assertIn("integrity level Low", text)
        self.assertIn("requires Medium", text)
        self.assertIn("no-write-up", text)
        self.assertIn("not an open file handle", text)


if __name__ == "__main__":
    unittest.main()
