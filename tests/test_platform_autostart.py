# -*- coding: utf-8 -*-
"""Current-user startup item (T15): HKCU Run key, no administrator needed."""

import os
import sys
import unittest
from unittest import mock

from src.platform.win.autostart import (
    Autostart,
    AutostartError,
    ENV_COMMAND,
    VALUE_NAME,
    default_command,
)

WINDOWS = sys.platform.startswith("win")
COMMAND = '"C:\\Apps\\DiskWarden\\DiskWarden.exe" --tray'


class FakeRegistry(object):
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.set_calls = []
        self.delete_calls = []

    def get(self, name):
        return self.values.get(name)

    def set(self, name, data):
        self.set_calls.append((name, data))
        self.values[name] = data

    def delete(self, name):
        self.delete_calls.append(name)
        if name in self.values:
            del self.values[name]
            return True
        return False


class BrokenRegistry(object):
    def get(self, name):
        raise AutostartError("cannot read", "AUTOSTART_FAILED")

    def set(self, name, data):
        raise AutostartError("cannot write", "AUTOSTART_FAILED")

    def delete(self, name):
        raise AutostartError("cannot delete", "AUTOSTART_FAILED")


class DefaultCommandTest(unittest.TestCase):
    def test_source_checkout_has_no_launch_command(self):
        with mock.patch.dict(os.environ, {ENV_COMMAND: ""}):
            self.assertEqual(default_command(), "")

    def test_environment_override_wins(self):
        with mock.patch.dict(os.environ, {ENV_COMMAND: COMMAND}):
            self.assertEqual(default_command(), COMMAND)

    @unittest.skipUnless(WINDOWS, "the packaged command is a Windows .exe")
    def test_frozen_build_registers_the_packaged_exe(self):
        with mock.patch.dict(os.environ, {ENV_COMMAND: ""}):
            with mock.patch.object(sys, "frozen", True, create=True):
                with mock.patch.object(sys, "executable", r"C:\Apps\DiskWarden\DiskWarden.exe"):
                    self.assertEqual(
                        default_command(),
                        '"C:\\Apps\\DiskWarden\\DiskWarden.exe" --tray',
                    )


@unittest.skipUnless(WINDOWS, "HKCU startup items are Windows-only")
class AutostartTest(unittest.TestCase):
    def make(self, values=None, command=COMMAND):
        registry = FakeRegistry(values)
        return Autostart(command=command, registry=registry), registry

    def test_supported_only_with_a_command(self):
        autostart, _registry = self.make()
        self.assertTrue(autostart.supported)
        unsupported, _registry = self.make(command="")
        self.assertFalse(unsupported.supported)

    def test_enable_writes_the_quoted_command(self):
        autostart, registry = self.make()
        self.assertEqual(autostart.enable(), COMMAND)
        self.assertEqual(registry.set_calls, [(VALUE_NAME, COMMAND)])
        self.assertTrue(autostart.is_enabled())

    def test_enable_without_a_command_is_unsupported(self):
        autostart, registry = self.make(command="")
        with self.assertRaises(AutostartError) as caught:
            autostart.enable()
        self.assertEqual(caught.exception.code, "AUTOSTART_UNSUPPORTED")
        self.assertEqual(registry.set_calls, [])

    def test_is_enabled_ignores_case_and_extra_spaces(self):
        autostart, _registry = self.make(
            {VALUE_NAME: '  "C:\\Apps\\DiskWarden\\DiskWarden.exe"   --tray '}
        )
        self.assertTrue(autostart.is_enabled())

    def test_stale_command_from_another_folder_is_not_enabled(self):
        autostart, _registry = self.make(
            {VALUE_NAME: '"C:\\Old\\DiskWarden.exe" --tray'}
        )
        self.assertFalse(autostart.is_enabled())
        self.assertEqual(autostart.registered(), '"C:\\Old\\DiskWarden.exe" --tray')

    def test_disable_removes_the_value(self):
        autostart, registry = self.make({VALUE_NAME: COMMAND})
        self.assertTrue(autostart.disable())
        self.assertEqual(registry.delete_calls, [VALUE_NAME])
        self.assertFalse(autostart.is_enabled())

    def test_disable_is_false_when_nothing_is_registered(self):
        autostart, _registry = self.make()
        self.assertFalse(autostart.disable())

    def test_status_reports_registry_state(self):
        autostart, _registry = self.make({VALUE_NAME: COMMAND})
        status = autostart.status()
        self.assertEqual(
            status,
            {
                "supported": True,
                "enabled": True,
                "command": COMMAND,
                "registered": COMMAND,
                "error": "",
            },
        )

    def test_status_of_unsupported_build(self):
        autostart, _registry = self.make(command="")
        self.assertEqual(
            autostart.status(),
            {
                "supported": False,
                "enabled": False,
                "command": "",
                "registered": "",
                "error": "",
            },
        )

    def test_status_surfaces_registry_errors(self):
        autostart = Autostart(command=COMMAND, registry=BrokenRegistry())
        status = autostart.status()
        self.assertTrue(status["supported"])
        self.assertFalse(status["enabled"])
        self.assertEqual(status["error"], "cannot read")

    def test_set_enabled_round_trip(self):
        autostart, _registry = self.make()
        self.assertTrue(autostart.set_enabled(True)["enabled"])
        self.assertFalse(autostart.set_enabled(False)["enabled"])

    def test_registry_failure_propagates(self):
        autostart = Autostart(command=COMMAND, registry=BrokenRegistry())
        with self.assertRaises(AutostartError):
            autostart.enable()


if __name__ == "__main__":
    unittest.main()
