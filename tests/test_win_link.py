# -*- coding: utf-8 -*-
"""Junction creation through ``DeviceIoControl`` (T26/R-01).

The old implementation shelled out to ``cmd /c mklink``, and ``cmd``
re-parses the rest of its command line: a target containing ``&``, ``|`` or
``^`` was truncated, the junction was created anyway and the migration
reported success against a path that did not exist.

These tests create real junctions in a temporary directory and read the
target back, which is exactly the assertion the P0 asks for.  Junctions need
NTFS, so the whole module skips itself when the filesystem cannot create one
(or when the platform is not Windows).
"""

import inspect
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

if not sys.platform.startswith("win"):
    raise unittest.SkipTest("junctions exist on Windows only")

from src.platform.link import LinkError, link_target_matches  # noqa: E402
from src.platform.win import link as win_link  # noqa: E402


# ``|`` is excluded: it is an illegal filename character on Windows and is
# covered separately as a clean-failure case.
SPECIAL_NAMES = (
    "a&ver&rem",
    "a^b",
    "a%b",
    "with space",
    "中文目录",
    "a&b^c%d e",
)


class JunctionCreationTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory(prefix="dw-t26-link-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.targets = self.root / "targets"
        self.links = self.root / "links"
        self.targets.mkdir()
        self.links.mkdir()
        probe_target = self.root / "probe-target"
        probe_target.mkdir()
        probe_link = self.root / "probe-link"
        try:
            win_link.create_junction(str(probe_link), str(probe_target))
        except (LinkError, OSError) as exc:
            self.skipTest("this filesystem has no junction support: %s" % exc)
        self.addCleanup(
            win_link.remove_junction, str(probe_link), str(probe_target)
        )

    def _target(self, name):
        target = self.targets / name
        target.mkdir()
        return target

    def test_special_characters_survive_in_target_and_link_names(self):
        for name in SPECIAL_NAMES:
            with self.subTest(name=name):
                target = self._target(name)
                payload = name.encode("utf-8")
                (target / "payload.bin").write_bytes(payload)
                link = self.links / name

                created = win_link.create_junction(str(link), str(target))

                self.assertEqual(
                    os.path.normcase(os.path.normpath(created)),
                    os.path.normcase(os.path.normpath(str(link))),
                )
                actual = win_link.read_junction_target(str(link))
                self.assertIsNotNone(actual)
                self.assertTrue(
                    link_target_matches(actual, str(target)),
                    "%r resolved to %r" % (str(target), actual),
                )
                self.assertEqual(
                    os.path.normcase(os.path.normpath(actual)),
                    os.path.normcase(os.path.normpath(str(target))),
                )
                # Not just a reparse point: the data is reachable through it.
                self.assertTrue(win_link.is_junction(str(link)))
                self.assertEqual((link / "payload.bin").read_bytes(), payload)
                self.assertTrue(
                    win_link.remove_junction(str(link), str(target))
                )
                self.assertFalse(os.path.lexists(str(link)))
                self.assertTrue(target.is_dir())

    def test_special_characters_in_a_nested_target(self):
        outer = self._target("a&b")
        target = outer / "c^d %e"
        target.mkdir()
        link = self.links / "j&k"

        win_link.create_junction(str(link), str(target))

        actual = win_link.read_junction_target(str(link))
        self.assertTrue(link_target_matches(actual, str(target)))

    def test_missing_target_is_a_link_error_without_leftovers(self):
        link = self.links / "missing-target"

        with self.assertRaises(LinkError):
            win_link.create_junction(str(link), str(self.targets / "absent"))

        self.assertFalse(os.path.lexists(str(link)))

    def test_existing_link_path_is_never_replaced(self):
        target = self._target("real")
        link = self.links / "taken"
        link.mkdir()
        keep = link / "keep.txt"
        keep.write_bytes(b"keep")

        with self.assertRaises(LinkError):
            win_link.create_junction(str(link), str(target))

        self.assertFalse(win_link.is_junction(str(link)))
        self.assertEqual(keep.read_bytes(), b"keep")

    def test_missing_parent_is_a_link_error(self):
        target = self._target("ok")
        link = self.links / "no-such-parent" / "link"

        with self.assertRaises(LinkError):
            win_link.create_junction(str(link), str(target))

        self.assertFalse(os.path.lexists(str(link)))

    def test_illegal_link_name_fails_without_leftovers(self):
        target = self._target("ok")
        link = self.links / "pipe|name"

        with self.assertRaises((LinkError, OSError)):
            win_link.create_junction(str(link), str(target))

        self.assertFalse(os.path.lexists(str(link)))

    def test_illegal_target_name_is_reported_as_a_link_error(self):
        link = self.links / "good-name"

        with self.assertRaises(LinkError):
            win_link.create_junction(
                str(link), str(self.targets / "pipe|target")
            )

        self.assertFalse(os.path.lexists(str(link)))

    def test_read_back_mismatch_removes_the_link(self):
        target = self._target("real")
        link = self.links / "mismatch"
        elsewhere = str(self.targets / "elsewhere")

        with mock.patch.object(
            win_link, "read_junction_target", return_value=elsewhere
        ):
            with self.assertRaises(LinkError) as caught:
                win_link.create_junction(str(link), str(target))

        self.assertIn("resolved to", str(caught.exception))
        self.assertFalse(os.path.lexists(str(link)))

    def test_device_io_failure_removes_the_link(self):
        target = self._target("real")
        link = self.links / "io-failure"

        with mock.patch.object(win_link, "_DeviceIoControl", return_value=0):
            with self.assertRaises(LinkError):
                win_link.create_junction(str(link), str(target))

        self.assertFalse(os.path.lexists(str(link)))

    def test_creation_never_uses_a_shell(self):
        source = inspect.getsource(win_link)
        self.assertNotIn("subprocess", source)
        self.assertNotIn("subprocess", vars(win_link))


if __name__ == "__main__":
    unittest.main()
