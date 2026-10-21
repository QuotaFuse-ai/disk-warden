# -*- coding: utf-8 -*-
"""macOS LaunchAgent adapter: plist contents, enable/disable, status (T21).

The plist is read with :mod:`plistlib`, so every assertion runs on any host.
"""

import os
import plistlib
import tempfile
import unittest
from unittest import mock

from src.platform.mac import autostart
from src.platform.mac.autostart import (
    LABEL,
    PLIST_NAME,
    Autostart,
    AutostartError,
    app_bundle_for,
    default_plist_path,
)

EXECUTABLE = "/Applications/Disk Warden.app/Contents/MacOS/DiskWarden"
COMMAND = '"%s" --tray' % EXECUTABLE


class AutostartTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = self.temp.name
        self.path = os.path.join(
            self.home, "Library", "LaunchAgents", PLIST_NAME
        )

    def _autostart(self, command=COMMAND):
        return Autostart(command=command, plist_path=self.path)

    def _payload(self):
        with open(self.path, "rb") as stream:
            return plistlib.load(stream)

    def test_default_path_lives_under_the_user_home(self):
        self.assertEqual(
            default_plist_path({"HOME": "/Users/alice"}),
            "/Users/alice/Library/LaunchAgents/" + PLIST_NAME,
        )

    def test_enable_writes_a_runnable_launch_agent(self):
        autostart = self._autostart()
        self.assertTrue(autostart.supported)
        self.assertFalse(autostart.is_enabled())

        self.assertEqual(autostart.enable(), COMMAND)

        payload = self._payload()
        self.assertEqual(payload["Label"], LABEL)
        self.assertEqual(payload["ProgramArguments"], [EXECUTABLE, "--tray"])
        self.assertTrue(payload["RunAtLoad"])
        self.assertFalse(payload["KeepAlive"])
        self.assertTrue(os.path.isfile(self.path))
        self.assertTrue(autostart.is_enabled())

    def test_registered_reports_the_stored_command(self):
        autostart = self._autostart()
        autostart.enable()
        self.assertEqual(autostart.registered(), COMMAND)
        self.assertEqual(
            autostart.status(),
            {
                "supported": True,
                "enabled": True,
                "command": COMMAND,
                "registered": COMMAND,
                "error": "",
            },
        )

    def test_disable_removes_the_plist_and_is_idempotent(self):
        autostart = self._autostart()
        autostart.enable()
        self.assertTrue(autostart.disable())
        self.assertFalse(os.path.exists(self.path))
        self.assertFalse(autostart.disable())
        self.assertFalse(autostart.is_enabled())

    def test_an_entry_from_another_install_is_not_enabled(self):
        autostart = self._autostart()
        autostart.enable()
        moved = self._autostart(
            command='"/Applications/Old DiskWarden.app/Contents/MacOS/DiskWarden" --tray'
        )
        self.assertFalse(moved.is_enabled())
        # ``registered`` reports what the plist holds (the first install),
        # while ``is_enabled`` is False because this build differs.
        self.assertEqual(moved.registered(), COMMAND)

    def test_source_checkout_has_no_command(self):
        autostart = self._autostart(command="")
        self.assertFalse(autostart.supported)
        self.assertFalse(autostart.is_enabled())
        with self.assertRaises(AutostartError) as ctx:
            autostart.enable()
        self.assertEqual(ctx.exception.code, "AUTOSTART_UNSUPPORTED")

    def test_corrupt_plist_reads_as_disabled(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "wb") as stream:
            stream.write(b"not a plist")
        autostart = self._autostart()
        self.assertFalse(autostart.is_enabled())
        status = autostart.status()
        self.assertFalse(status["enabled"])
        self.assertEqual(status["registered"], "")

    def test_unwritable_location_is_reported(self):
        # ``Library`` is a file, so ``Library/LaunchAgents`` cannot be created.
        with open(os.path.join(self.home, "Library"), "wb") as stream:
            stream.write(b"x")
        autostart = Autostart(command=COMMAND, plist_path=self.path)
        with self.assertRaises(AutostartError):
            autostart.enable()

    def test_set_enabled_round_trips(self):
        autostart = self._autostart()
        self.assertTrue(autostart.set_enabled(True)["enabled"])
        self.assertFalse(autostart.set_enabled(False)["enabled"])


class DefaultCommandTest(unittest.TestCase):
    """The login item must start the bundle, not the bare binary inside it."""

    def _command(self, executable, frozen=True):
        with mock.patch.dict(os.environ, {autostart.ENV_COMMAND: ""}), mock.patch.object(
            autostart.sys, "frozen", frozen, create=True
        ), mock.patch.object(autostart.sys, "executable", executable):
            return autostart.default_command()

    def test_app_bundle_is_detected(self):
        # Detection is structural: anything under ``Contents/MacOS`` belongs to
        # the bundle two levels up, whatever the app or binary is called.
        self.assertEqual(app_bundle_for(EXECUTABLE), "/Applications/Disk Warden.app")
        self.assertEqual(app_bundle_for("/Applications/Thing.app/Contents/MacOS/x"), "/Applications/Thing.app")
        self.assertEqual(app_bundle_for("/opt/DiskWarden.app/Contents/MacOS/DiskWarden"), "/opt/DiskWarden.app")
        self.assertEqual(app_bundle_for("/usr/local/bin/DiskWarden"), "")
        self.assertEqual(app_bundle_for("/Applications/Thing.app/Contents/Resources/x"), "")
        self.assertEqual(app_bundle_for(""), "")

    def test_bundle_is_started_through_open(self):
        self.assertEqual(
            self._command(EXECUTABLE),
            '/usr/bin/open -n "/Applications/Disk Warden.app" --args --tray',
        )

    def test_plain_executable_falls_back_to_a_direct_start(self):
        self.assertEqual(
            self._command("/opt/disk-warden/DiskWarden"),
            '"/opt/disk-warden/DiskWarden" --tray',
        )

    def test_source_checkout_registers_nothing(self):
        self.assertEqual(self._command("/usr/bin/python3", frozen=False), "")

    def test_environment_override_wins(self):
        with mock.patch.dict(os.environ, {autostart.ENV_COMMAND: '"x" --tray'}):
            self.assertEqual(autostart.default_command(), '"x" --tray')

    def test_frozen_bundle_writes_the_open_command(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = os.path.join(temp.name, "agent.plist")
        with mock.patch.dict(os.environ, {autostart.ENV_COMMAND: ""}), mock.patch.object(
            autostart.sys, "frozen", True, create=True
        ), mock.patch.object(autostart.sys, "executable", EXECUTABLE):
            controller = Autostart(plist_path=path)
            controller.enable()
        with open(path, "rb") as stream:
            arguments = plistlib.load(stream)["ProgramArguments"]
        self.assertEqual(
            arguments,
            [
                "/usr/bin/open",
                "-n",
                "/Applications/Disk Warden.app",
                "--args",
                "--tray",
            ],
        )
        self.assertTrue(controller.is_enabled())


if __name__ == "__main__":
    unittest.main()
