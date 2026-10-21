# -*- coding: utf-8 -*-
"""The plan precheck asks the platform for link support (T21).

The business layer must not test ``os.name``/``is_ntfs`` itself, so these
tests patch the platform function and assert the bridge error it produces.
"""

import unittest
from unittest import mock

from src.app.errors import BridgeError
from tests.app_fakes import make_item, make_service, set_snapshot


class PlanLinkSupportTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service(self)

    def _preview(self):
        item = make_item("cursor-cache")
        set_snapshot(self.service, [item])
        return self.service.plan_preview([item["item_id"]])

    def test_unsupported_filesystem_carries_the_platform_code(self):
        support = {
            "supported": False,
            "code": "TARGET_FS_UNSUPPORTED",
            "message": "the target disk must be APFS or HFS+ to create links",
            "fs_name": "exfat",
            "mount_point": "/Volumes/USB",
        }
        with mock.patch("src.app.service.link_support", return_value=support):
            with self.assertRaises(BridgeError) as ctx:
                self._preview()

        error = ctx.exception
        self.assertEqual(error.code, "TARGET_FS_UNSUPPORTED")
        self.assertFalse(error.retryable)
        self.assertEqual(error.detail["fs"], "exfat")
        self.assertEqual(error.detail["mount_point"], "/Volumes/USB")
        self.assertEqual(len(self.service.plans._plans), 0)

    def test_windows_ntfs_requirement_still_reaches_the_page(self):
        support = {
            "supported": False,
            "code": "TARGET_NOT_NTFS",
            "message": "the target disk must be NTFS to create links",
            "fs_name": "exFAT",
            "mount_point": "E:\\",
        }
        with mock.patch("src.app.service.link_support", return_value=support):
            with self.assertRaises(BridgeError) as ctx:
                self._preview()
        self.assertEqual(ctx.exception.code, "TARGET_NOT_NTFS")

    def test_probe_failure_is_advisory(self):
        with mock.patch(
            "src.app.service.link_support", side_effect=OSError("no statfs")
        ):
            plan = self._preview()
        self.assertIn("plan_id", plan)

    def test_supported_filesystem_plans_normally(self):
        support = {
            "supported": True,
            "code": "",
            "message": "",
            "fs_name": "apfs",
            "mount_point": "/",
        }
        with mock.patch("src.app.service.link_support", return_value=support):
            plan = self._preview()
        self.assertIn("plan_id", plan)


if __name__ == "__main__":
    unittest.main()
