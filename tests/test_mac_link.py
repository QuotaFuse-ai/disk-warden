# -*- coding: utf-8 -*-
"""macOS link/volume adapter, exercised off-macOS where possible (T21)."""

import os
import tempfile
import unittest
from unittest import mock

from src.platform.link import LinkError, VolumeInfo
from src.platform.mac import link as mac_link


def _can_symlink(root):
    """Windows needs Developer Mode/privilege; skip link tests without it."""
    link_path = os.path.join(root, "probe-link")
    try:
        os.symlink(os.path.join(root, "probe-target"), link_path)
    except (OSError, NotImplementedError):
        return False
    os.unlink(link_path)
    return True


class VolumeInfoTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name

    def test_statfs_identity_is_used_when_available(self):
        probe = mac_link._Statfs()
        probe.f_mntonname = b"/System/Volumes/Data\0"
        probe.f_fstypename = b"apfs\0"
        with mock.patch.object(mac_link, "_probe", return_value=probe):
            info = mac_link.volume_info(self.root)

        self.assertEqual(info.mount_point, "/System/Volumes/Data")
        self.assertEqual(info.fs_name, "apfs")
        self.assertEqual(info.serial, int(os.lstat(self.root).st_dev))

    def test_mount_point_walk_is_the_fallback(self):
        with mock.patch.object(
            mac_link, "_probe", side_effect=RuntimeError("not macOS")
        ):
            info = mac_link.volume_info(self.root)

        self.assertEqual(info.mount_point, mac_link._mount_point(self.root))
        self.assertEqual(info.fs_name, "")
        self.assertEqual(info.serial, int(os.lstat(self.root).st_dev))

    def test_missing_path_raises_link_error(self):
        missing = os.path.join(self.root, "missing")
        with self.assertRaises(LinkError):
            mac_link.volume_info(missing)

    def test_apfs_supports_links(self):
        info = VolumeInfo("/", "apfs", 7)
        with mock.patch.object(mac_link, "volume_info", return_value=info):
            support = mac_link.link_support(self.root)
        self.assertTrue(support["supported"])
        self.assertEqual(support["fs_name"], "apfs")

    def test_exfat_is_reported_before_any_copy(self):
        info = VolumeInfo("/Volumes/USB", "exfat", 9)
        with mock.patch.object(mac_link, "volume_info", return_value=info):
            support = mac_link.link_support("/Volumes/USB")
        self.assertFalse(support["supported"])
        self.assertEqual(support["code"], "TARGET_FS_UNSUPPORTED")
        self.assertIn("APFS", support["message"])

    def test_unknown_filesystem_is_not_guessed_against(self):
        info = VolumeInfo("/", "", 1)
        with mock.patch.object(mac_link, "volume_info", return_value=info):
            support = mac_link.link_support(self.root)
        self.assertTrue(support["supported"])


class SymlinkTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name
        if not _can_symlink(self.root):
            self.skipTest("creating symlinks needs Developer Mode on Windows")
        self.target = os.path.join(self.root, "target")
        os.mkdir(self.target)
        with open(os.path.join(self.target, "data.bin"), "wb") as stream:
            stream.write(b"payload")
        self.link = os.path.join(self.root, "source")

    def test_create_reads_the_target_back(self):
        created = mac_link.create_junction(self.link, self.target)

        self.assertEqual(created, self.link)
        self.assertTrue(os.path.islink(self.link))
        self.assertTrue(mac_link.is_junction(self.link))
        self.assertTrue(mac_link.is_link(self.link))
        self.assertEqual(
            mac_link.read_junction_target(self.link),
            os.path.abspath(self.target),
        )
        with open(os.path.join(self.link, "data.bin"), "rb") as stream:
            self.assertEqual(stream.read(), b"payload")

    def test_create_refuses_an_existing_path(self):
        os.mkdir(self.link)
        with self.assertRaises(LinkError):
            mac_link.create_junction(self.link, self.target)

    def test_create_refuses_a_missing_target(self):
        with self.assertRaises(LinkError):
            mac_link.create_junction(
                self.link, os.path.join(self.root, "nope")
            )

    def test_read_back_mismatch_removes_only_the_link(self):
        other = os.path.join(self.root, "other")
        os.mkdir(other)
        real_symlink = os.symlink

        def wrong_target(source, destination, target_is_directory=False):
            real_symlink(other, destination, target_is_directory=target_is_directory)

        with mock.patch.object(mac_link.os, "symlink", side_effect=wrong_target):
            with self.assertRaises(LinkError):
                mac_link.create_junction(self.link, self.target)

        self.assertFalse(os.path.lexists(self.link))
        self.assertTrue(os.path.isdir(self.target))
        with open(os.path.join(self.target, "data.bin"), "rb") as stream:
            self.assertEqual(stream.read(), b"payload")

    def test_remove_unlinks_without_touching_the_target(self):
        mac_link.create_junction(self.link, self.target)

        self.assertTrue(mac_link.remove_junction(self.link, self.target))

        self.assertFalse(os.path.lexists(self.link))
        self.assertTrue(os.path.isdir(self.target))
        with open(os.path.join(self.target, "data.bin"), "rb") as stream:
            self.assertEqual(stream.read(), b"payload")

    def test_remove_is_idempotent(self):
        self.assertFalse(mac_link.remove_junction(self.link))

    def test_remove_refuses_a_real_directory(self):
        with self.assertRaises(LinkError):
            mac_link.remove_junction(self.target)
        self.assertTrue(os.path.isdir(self.target))

    def test_remove_refuses_a_link_pointing_elsewhere(self):
        other = os.path.join(self.root, "other")
        os.mkdir(other)
        mac_link.create_junction(self.link, other)

        with self.assertRaises(LinkError):
            mac_link.remove_junction(self.link, self.target)

        self.assertTrue(os.path.islink(self.link))
        self.assertTrue(os.path.isdir(other))

    def test_rename_dir_refuses_to_overwrite(self):
        destination = os.path.join(self.root, "occupied")
        os.mkdir(destination)
        with self.assertRaises(LinkError):
            mac_link.rename_dir(self.target, destination)


if __name__ == "__main__":
    unittest.main()
