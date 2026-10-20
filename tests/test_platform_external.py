# -*- coding: utf-8 -*-
"""Default-browser hand-off; ``webbrowser.open`` is patched in every test."""

import unittest
from unittest import mock

from src.platform.external import ExternalOpenError, open_url

URL = "https://github.com/QuotaFuse-ai/disk-warden/issues/new"


class OpenUrlTest(unittest.TestCase):
    def test_url_is_opened_in_a_new_tab(self):
        with mock.patch(
            "src.platform.external.webbrowser.open", return_value=True
        ) as opener:
            open_url(URL)
        opener.assert_called_once_with(URL, new=2, autoraise=True)

    def test_falsy_return_means_no_browser_took_the_url(self):
        with mock.patch(
            "src.platform.external.webbrowser.open", return_value=False
        ):
            with self.assertRaises(ExternalOpenError):
                open_url(URL)

    def test_backend_exception_is_wrapped(self):
        with mock.patch(
            "src.platform.external.webbrowser.open",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(ExternalOpenError):
                open_url(URL)


if __name__ == "__main__":
    unittest.main()
