# -*- coding: utf-8 -*-
"""macOS version detection and the first-launch guide data (T21).

The unsigned ``.app`` is stopped by Gatekeeper once, and the way out differs
by OS version: macOS 15 dropped the right-click bypass.  These tests pin the
data contract (tier, step ids, advanced command) that the page renders; they
run on any host because no macOS API is called with a fixed version.
"""

import json
import sys
import unittest
from unittest import mock

from src.platform.mac import version as mac_version
from src.platform.mac.version import (
    FALLBACK_BUNDLE,
    MIN_SUPPORTED_MAJOR,
    STEPS,
    TIER_RIGHT_CLICK,
    TIER_SETTINGS_PANE,
    TIER_UNKNOWN,
    bundle_path,
    first_launch_guide,
    parse_version,
    quarantine_command,
    version_text,
)


class ParseVersionTest(unittest.TestCase):
    def test_a_plain_version_splits_into_three_numbers(self):
        self.assertEqual(parse_version("15.1.1"), (15, 1, 1))

    def test_a_missing_patch_is_zero(self):
        self.assertEqual(parse_version("14"), (14, 0, 0))
        self.assertEqual(parse_version("13.6"), (13, 6, 0))

    def test_a_build_suffix_is_ignored(self):
        self.assertEqual(parse_version("15.1 (24B83)"), (15, 1, 0))

    def test_garbage_is_unknown_not_an_exception(self):
        for value in ("", None, "Sequoia", "...", "x.15"):
            self.assertEqual(parse_version(value), (0, 0, 0), value)

    def test_only_the_first_three_parts_are_kept(self):
        self.assertEqual(parse_version("12.6.1.9"), (12, 6, 1))


class VersionTextTest(unittest.TestCase):
    def test_minor_zero_still_reads_as_a_dotted_version(self):
        self.assertEqual(version_text((15, 0, 0)), "15.0")

    def test_patch_is_only_shown_when_present(self):
        self.assertEqual(version_text((14, 5, 0)), "14.5")
        self.assertEqual(version_text((14, 5, 1)), "14.5.1")

    def test_unknown_has_no_text(self):
        self.assertEqual(version_text((0, 0, 0)), "")


class MacosVersionTest(unittest.TestCase):
    def test_platform_mac_ver_is_the_primary_source(self):
        with mock.patch.object(
            mac_version.platform, "mac_ver", return_value=("15.2", ("", "", ""), "")
        ):
            with mock.patch.object(mac_version, "_sw_vers") as fallback:
                self.assertEqual(mac_version.macos_version(), (15, 2, 0))
        fallback.assert_not_called()

    def test_sw_vers_is_the_fallback_when_the_plist_is_unreadable(self):
        with mock.patch.object(mac_version.platform, "mac_ver", return_value=("", "", "")):
            with mock.patch.object(mac_version, "_sw_vers", return_value="14.6.1"):
                self.assertEqual(mac_version.macos_version(), (14, 6, 1))

    @unittest.skipIf(sys.platform == "darwin", "off-macOS behaviour")
    def test_no_mac_is_unknown(self):
        self.assertEqual(mac_version._sw_vers(), "")
        self.assertEqual(mac_version.macos_version(), (0, 0, 0))


class QuarantineCommandTest(unittest.TestCase):
    def test_the_bundle_path_is_single_quoted(self):
        command = quarantine_command("/Applications/Disk Warden.app")
        self.assertEqual(
            command,
            "xattr -dr com.apple.quarantine '/Applications/Disk Warden.app'",
        )

    def test_a_quote_in_the_path_cannot_break_out(self):
        command = quarantine_command("/tmp/it's here.app")
        self.assertIn("'/tmp/it'\\''s here.app'", command)

    def test_without_a_bundle_the_conventional_path_is_used(self):
        with mock.patch.object(mac_version, "bundle_path", return_value=FALLBACK_BUNDLE):
            self.assertIn("'%s'" % FALLBACK_BUNDLE, quarantine_command())

    def test_bundle_path_falls_back_outside_a_bundle(self):
        with mock.patch.object(sys, "executable", "/usr/local/bin/python3"):
            self.assertEqual(bundle_path(), FALLBACK_BUNDLE)

    def test_bundle_path_reads_the_running_app(self):
        executable = "/Applications/Disk Warden.app/Contents/MacOS/DiskWarden"
        with mock.patch.object(sys, "executable", executable):
            self.assertEqual(bundle_path(), "/Applications/Disk Warden.app")


class FirstLaunchGuideTest(unittest.TestCase):
    def test_macos_15_uses_the_privacy_security_path(self):
        guide = first_launch_guide(version=(15, 1, 0))
        self.assertEqual(guide["tier"], TIER_SETTINGS_PANE)
        self.assertEqual(guide["steps"], list(STEPS[TIER_SETTINGS_PANE]))
        self.assertIn("open_privacy_security", guide["steps"])
        self.assertIn("allow_anyway", guide["steps"])
        self.assertNotIn("control_click_open", guide["steps"])
        self.assertEqual(guide["version"], "15.1")
        self.assertEqual(guide["major"], 15)

    def test_macos_26_also_uses_the_privacy_security_path(self):
        guide = first_launch_guide(version=(26, 0, 0))
        self.assertEqual(guide["tier"], TIER_SETTINGS_PANE)

    def test_ventura_through_sonoma_uses_control_click(self):
        for version in ((13, 6, 7), (14, 7, 2)):
            guide = first_launch_guide(version=version)
            self.assertEqual(guide["tier"], TIER_RIGHT_CLICK, version)
            self.assertEqual(guide["steps"], list(STEPS[TIER_RIGHT_CLICK]))
            self.assertIn("control_click_open", guide["steps"])
            self.assertNotIn("allow_anyway", guide["steps"])

    def test_an_unknown_version_keeps_the_new_path_and_says_so(self):
        guide = first_launch_guide(version=(0, 0, 0))
        self.assertEqual(guide["tier"], TIER_UNKNOWN)
        self.assertEqual(guide["steps"], list(STEPS[TIER_SETTINGS_PANE]))
        self.assertEqual(guide["version"], "")
        self.assertTrue(guide["supported"])

    def test_an_unsupported_old_mac_is_flagged(self):
        guide = first_launch_guide(version=(10, 15, 7))
        self.assertFalse(guide["supported"])
        self.assertEqual(guide["min_supported"], MIN_SUPPORTED_MAJOR)

    def test_supported_starts_at_big_sur(self):
        self.assertTrue(first_launch_guide(version=(11, 0, 0))["supported"])

    def test_the_guide_shows_until_it_is_acknowledged(self):
        self.assertTrue(first_launch_guide(version=(14, 5, 0))["show"])
        self.assertFalse(
            first_launch_guide(first_run_done=True, version=(14, 5, 0))["show"]
        )

    def test_the_payload_is_json_safe(self):
        json.dumps(first_launch_guide(version=(15, 0, 0)))

    def test_a_version_string_is_accepted(self):
        self.assertEqual(first_launch_guide(version="14.4")["tier"], TIER_RIGHT_CLICK)


if __name__ == "__main__":
    unittest.main()
