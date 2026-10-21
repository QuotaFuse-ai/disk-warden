# -*- coding: utf-8 -*-
"""The first-launch guide reaches the page as plain settings data (T21).

The page must not know which OS it runs on, so ``AppService`` embeds whatever
``src.platform.first_launch_guide`` returns under ``settings.first_launch``
and stores the one bit the user can change (``first_run_done``).
"""

import sys
import unittest
from unittest import mock

from src.app.errors import BridgeError
from tests.app_fakes import make_service

DARWIN_GUIDE = {
    "show": True,
    "platform": "darwin",
    "version": "15.1",
    "major": 15,
    "tier": "settings_pane",
    "steps": ["move_to_applications", "open_privacy_security", "allow_anyway"],
    "advanced_command": "xattr -dr com.apple.quarantine '/Applications/Disk Warden.app'",
    "min_supported": 11,
    "supported": True,
}


class FirstLaunchSettingsTest(unittest.TestCase):
    def test_settings_get_embeds_the_platform_guide(self):
        service = make_service(self)
        with mock.patch(
            "src.app.service.first_launch_guide", return_value=dict(DARWIN_GUIDE)
        ) as guide:
            payload = service.settings_get()
        guide.assert_called_once_with(False)
        self.assertEqual(payload["first_launch"], DARWIN_GUIDE)

    def test_acknowledging_persists_and_hides_the_guide(self):
        service = make_service(self)
        with mock.patch(
            "src.app.service.first_launch_guide", return_value=dict(DARWIN_GUIDE)
        ) as guide:
            service.settings_set({"first_run_done": True})
            service.settings_get()
        guide.assert_called_with(True)
        self.assertTrue(service.settings_store.load()["first_run_done"])

    def test_the_flag_is_saved_next_to_the_other_settings(self):
        service = make_service(self)
        service.settings_set({"first_run_done": True})
        stored = service.settings_store.load()
        self.assertIs(stored["first_run_done"], True)
        # A second unrelated patch must not drop it.
        service.settings_set({"language": "zh-CN"})
        self.assertTrue(service.settings_store.load()["first_run_done"])

    def test_a_non_boolean_flag_is_rejected(self):
        service = make_service(self)
        for value in ("yes", 1, None):
            with self.assertRaises(BridgeError) as caught:
                service.settings_set({"first_run_done": value})
            self.assertEqual(caught.exception.code, "INVALID_ARGUMENT", value)

    def test_hello_carries_the_guide_without_an_extra_call(self):
        service = make_service(self)
        hello = service.hello()
        self.assertIn("first_launch", hello["settings"])

    @unittest.skipIf(sys.platform == "darwin", "the stub is for other hosts")
    def test_non_macos_hosts_never_show_the_dialog(self):
        service = make_service(self)
        guide = service.settings_get()["first_launch"]
        self.assertFalse(guide["show"])
        self.assertEqual(guide["steps"], [])
        self.assertEqual(guide["tier"], "none")


if __name__ == "__main__":
    unittest.main()
