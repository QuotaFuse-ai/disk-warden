# -*- coding: utf-8 -*-
"""``AppService.open_external``: whitelisted browser hand-off."""

import unittest
from unittest import mock

from src.app.errors import BridgeError
from tests.app_fakes import make_service

ALLOWED = "https://github.com/QuotaFuse-ai/disk-warden/issues/new"
DEVICE = "https://github.com/login/device"


class OpenExternalTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service(self)

    def open_url(self, url):
        with mock.patch("src.app.service.open_url") as opener:
            result = self.service.open_external(url)
        return result, opener

    def test_allowed_url_is_handed_to_the_browser(self):
        result, opener = self.open_url(ALLOWED)
        self.assertEqual(result, {"opened": True, "url": ALLOWED})
        opener.assert_called_once_with(ALLOWED)

    def test_repo_root_and_port_443_are_allowed(self):
        for url in (
            "https://github.com/QuotaFuse-ai/disk-warden",
            "https://github.com:443/QuotaFuse-ai/disk-warden",
        ):
            result, opener = self.open_url(url)
            self.assertEqual(result["url"], url)
            opener.assert_called_once_with(url)

    def test_device_verification_page_is_allowed(self):
        # T16: the welcome card's "打开授权页面" button and auth.start both
        # hand GitHub's device page to the browser.
        for url in (
            DEVICE,
            DEVICE + "/",
            DEVICE + "?user_code=ABCD-1234",
        ):
            result, opener = self.open_url(url)
            self.assertEqual(result["url"], url)
            opener.assert_called_once_with(url)

    def test_other_github_login_pages_are_forbidden(self):
        for url in (
            "https://github.com/login/oauth/authorize",
            "https://github.com/login/device-callback",
            "https://github.com/login",
            "https://github.com/settings/tokens",
            "http://github.com/login/device",
            "https://evil.com/login/device",
        ):
            with mock.patch("src.app.service.open_url") as opener:
                with self.assertRaises(BridgeError) as caught:
                    self.service.open_external(url)
            self.assertEqual(caught.exception.code, "FORBIDDEN", url)
            opener.assert_not_called()

    def test_other_schemes_and_hosts_are_forbidden(self):
        rejected = (
            "http://github.com/QuotaFuse-ai/disk-warden",
            "file:///C:/Windows/System32/calc.exe",
            "javascript:alert(1)",
            "https://evil.com/QuotaFuse-ai/disk-warden",
            "https://github.com/evil-org/disk-warden",
            "https://github.com/QuotaFuse-ai",
            "https://github.com/QuotaFuse-ai/../evil",
            "https://github.com/QuotaFuse-ai%2f..%2fevil",
            "https://user@github.com/QuotaFuse-ai/disk-warden",
            "https://github.com:8443/QuotaFuse-ai/disk-warden",
        )
        for url in rejected:
            with mock.patch("src.app.service.open_url") as opener:
                with self.assertRaises(BridgeError) as caught:
                    self.service.open_external(url)
            self.assertEqual(caught.exception.code, "FORBIDDEN", url)
            opener.assert_not_called()

    def test_invalid_arguments_are_rejected(self):
        cases = (
            "",
            None,
            7,
            ["https://github.com/QuotaFuse-ai/disk-warden"],
            "https://github.com/QuotaFuse-ai/disk\nwarden",
            "https://github.com/QuotaFuse-ai/" + "a" * 2048,
        )
        for url in cases:
            with mock.patch("src.app.service.open_url") as opener:
                with self.assertRaises(BridgeError) as caught:
                    self.service.open_external(url)
            self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")
            opener.assert_not_called()

    def test_browser_failure_maps_to_io_error(self):
        with mock.patch(
            "src.app.service.open_url",
            side_effect=OSError("no browser"),
        ):
            with self.assertRaises(BridgeError) as caught:
                self.service.open_external(ALLOWED)
        self.assertEqual(caught.exception.code, "IO_ERROR")
        self.assertEqual(caught.exception.detail["url"], ALLOWED)


if __name__ == "__main__":
    unittest.main()
