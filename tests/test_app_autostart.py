# -*- coding: utf-8 -*-
"""``AppService`` startup-item wiring (T15).

The service must mirror the registry, not just the settings file: the switch in
the UI has to reflect whether this exact install starts with Windows.
"""

import unittest

from src.app.errors import BridgeError
from src.platform.win.autostart import AutostartError
from tests.app_fakes import make_service

COMMAND = '"C:\\Apps\\DiskWarden\\DiskWarden.exe" --tray'


class FakeAutostart(object):
    def __init__(self, supported=True, enabled=False, command=COMMAND, error=None):
        self.supported = supported
        self.enabled = enabled
        self.command = command
        self.error = error
        self.ignore_writes = False
        self.calls = []

    def status(self):
        if self.error is not None:
            raise self.error
        return {
            "supported": self.supported,
            "enabled": self.enabled,
            "command": self.command,
            "registered": self.command if self.enabled else "",
            "error": "",
        }

    def set_enabled(self, enabled):
        self.calls.append(enabled)
        if self.error is not None:
            raise self.error
        self.enabled = bool(enabled) and not self.ignore_writes
        return self.status()


class AutostartServiceTest(unittest.TestCase):
    def test_settings_report_the_registry_state(self):
        fake = FakeAutostart(enabled=True)
        service = make_service(self, autostart=fake)
        payload = service.settings_get()
        self.assertTrue(payload["launch_at_startup"])
        self.assertTrue(payload["launch_at_startup_supported"])
        self.assertEqual(payload["launch_at_startup_command"], COMMAND)
        self.assertEqual(payload["launch_at_startup_error"], "")

    def test_enabling_writes_the_registry_and_the_settings_file(self):
        fake = FakeAutostart()
        service = make_service(self, autostart=fake)
        result = service.settings_set({"launch_at_startup": True})
        self.assertEqual(fake.calls, [True])
        self.assertTrue(result["launch_at_startup"])
        self.assertTrue(service.settings_store.load()["launch_at_startup"])

    def test_disabling_removes_the_registry_value(self):
        fake = FakeAutostart(enabled=True)
        service = make_service(self, autostart=fake)
        result = service.settings_set({"launch_at_startup": False})
        self.assertEqual(fake.calls, [False])
        self.assertFalse(result["launch_at_startup"])

    def test_unsupported_build_only_stores_the_preference(self):
        fake = FakeAutostart(supported=False, command="")
        service = make_service(self, autostart=fake)
        self.assertFalse(service.settings_get()["launch_at_startup_supported"])
        result = service.settings_set({"launch_at_startup": True})
        self.assertEqual(fake.calls, [])
        self.assertTrue(result["launch_at_startup"])
        self.assertTrue(service.settings_store.load()["launch_at_startup"])

    def test_registry_errors_surface_as_autostart_failed(self):
        fake = FakeAutostart(error=AutostartError("access denied"))
        service = make_service(self, autostart=fake)
        with self.assertRaises(BridgeError) as caught:
            service.settings_set({"launch_at_startup": True})
        self.assertEqual(caught.exception.code, "AUTOSTART_FAILED")
        self.assertFalse(service.settings_store.load()["launch_at_startup"])

    def test_unexpected_errors_are_mapped_too(self):
        class Explodes(FakeAutostart):
            def set_enabled(self, enabled):
                self.calls.append(enabled)
                raise RuntimeError("boom")

        service = make_service(self, autostart=Explodes())
        with self.assertRaises(BridgeError) as caught:
            service.settings_set({"launch_at_startup": True})
        self.assertEqual(caught.exception.code, "AUTOSTART_FAILED")

    def test_silently_ignored_write_is_reported(self):
        fake = FakeAutostart()
        fake.ignore_writes = True
        service = make_service(self, autostart=fake)
        with self.assertRaises(BridgeError) as caught:
            service.settings_set({"launch_at_startup": True})
        self.assertEqual(caught.exception.code, "AUTOSTART_FAILED")

    def test_status_errors_do_not_break_settings(self):
        fake = FakeAutostart(error=AutostartError("cannot read"))
        service = make_service(self, autostart=fake)
        payload = service.settings_get()
        self.assertFalse(payload["launch_at_startup_supported"])
        self.assertIn("cannot read", payload["launch_at_startup_error"])

    def test_non_boolean_is_rejected(self):
        service = make_service(self, autostart=FakeAutostart())
        with self.assertRaises(BridgeError) as caught:
            service.settings_set({"launch_at_startup": "yes"})
        self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")


if __name__ == "__main__":
    unittest.main()
