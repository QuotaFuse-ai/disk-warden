# -*- coding: utf-8 -*-
"""Startup-item adapter for platforms without a per-user Run key yet.

macOS (``LaunchAgents``) and Linux (XDG autostart) are follow-ups; until then
the switch reports itself as unsupported instead of pretending to work.
"""


class AutostartError(Exception):
    """The startup item could not be read or written."""

    def __init__(self, message, code="AUTOSTART_UNSUPPORTED"):
        # type: (str, str) -> None
        Exception.__init__(self, message)
        self.code = code


class Autostart(object):
    """No-op implementation of the Windows :class:`Autostart` interface."""

    def __init__(self, command=None, registry=None, value_name="DiskWarden", environ=None):
        # type: (object, object, str, object) -> None
        self._command = "" if command is None else str(command)
        self.value_name = value_name

    @property
    def supported(self):
        # type: () -> bool
        return False

    @property
    def command(self):
        # type: () -> str
        return self._command

    def registered(self):
        # type: () -> str
        return ""

    def is_enabled(self):
        # type: () -> bool
        return False

    def enable(self):
        # type: () -> str
        raise AutostartError(
            "startup items are not implemented on this platform",
            "AUTOSTART_UNSUPPORTED",
        )

    def disable(self):
        # type: () -> bool
        return False

    def status(self):
        # type: () -> dict
        return {
            "supported": False,
            "enabled": False,
            "command": self._command,
            "registered": "",
            "error": "",
        }

    def set_enabled(self, enabled):
        # type: (bool) -> dict
        if enabled:
            self.enable()
        return self.status()


__all__ = ["Autostart", "AutostartError"]
