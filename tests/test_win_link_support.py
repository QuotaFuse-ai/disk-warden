# -*- coding: utf-8 -*-
"""Windows ``link_support`` contract used by the plan precheck (T21)."""

import sys
import unittest
from unittest import mock

from src.platform.link import VolumeInfo

if not sys.platform.startswith("win"):
    raise unittest.SkipTest("the Windows adapter is imported on Windows only")

from src.platform.win import link as win_link  # noqa: E402


class WinLinkSupportTest(unittest.TestCase):
    def test_ntfs_supports_junctions(self):
        info = VolumeInfo("D:\\", "NTFS", 2020)
        with mock.patch.object(win_link, "volume_info", return_value=info):
            support = win_link.link_support("D:\\DiskWarden")
        self.assertTrue(support["supported"])
        self.assertEqual(support["mount_point"], "D:\\")

    def test_exfat_is_rejected_with_the_existing_bridge_code(self):
        info = VolumeInfo("E:\\", "exFAT", 7)
        with mock.patch.object(win_link, "volume_info", return_value=info):
            support = win_link.link_support("E:\\DiskWarden")
        self.assertFalse(support["supported"])
        self.assertEqual(support["code"], "TARGET_NOT_NTFS")
        self.assertIn("NTFS", support["message"])


if __name__ == "__main__":
    unittest.main()
