# -*- coding: utf-8 -*-
"""Current-user startup item (``HKCU\\...\\Run``), no administrator needed.

T15 requires the "start with Windows" switch to write the per-user startup
item instead of asking for elevation.  ``HKCU`` is writable by the signed-in
user, so :class:`Autostart` only ever touches

    HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run

and the value is a quoted command line.  A frozen build registers
``"<exe>" --tray``; a source checkout is left unsupported on purpose so that
running the tests never edits the developer's real startup entries.

Python 3.8 compatible (decision 0005); the import of ``winreg`` is guarded so
the module can be imported on any platform for tests.
"""

import os
import sys

try:  # pragma: no cover - exercised on Windows only
    import winreg
except ImportError:  # pragma: no cover
    winreg = None

RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "DiskWarden"
STARTUP_ARG = "--tray"
ENV_COMMAND = "DISKWARDEN_AUTOSTART_COMMAND"


class AutostartError(Exception):
    """The startup item could not be read or written."""

    def __init__(self, message, code="AUTOSTART_FAILED"):
        # type: (str, str) -> None
        Exception.__init__(self, message)
        self.code = code


class WinRegistry(object):
    """Thin wrapper over ``winreg``; injectable so tests stay platform-neutral."""

    def __init__(self, root=None, path=RUN_KEY_PATH):
        # type: (object, str) -> None
        self.root = winreg.HKEY_CURRENT_USER if root is None else root
        self.path = path

    def get(self, name):
        # type: (str) -> object
        if winreg is None:
            raise AutostartError("winreg is unavailable", "AUTOSTART_UNSUPPORTED")
        try:
            with winreg.OpenKey(self.root, self.path, 0, winreg.KEY_READ) as key:
                value, _kind = winreg.QueryValueEx(key, name)
                return value
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise AutostartError(
                "cannot read %s\\%s: %s" % (self.path, name, exc)
            )

    def set(self, name, data):
        # type: (str, str) -> None
        if winreg is None:
            raise AutostartError("winreg is unavailable", "AUTOSTART_UNSUPPORTED")
        try:
            with winreg.CreateKeyEx(
                self.root, self.path, 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, data)
        except OSError as exc:
            raise AutostartError(
                "cannot write %s\\%s: %s" % (self.path, name, exc)
            )

    def delete(self, name):
        # type: (str) -> bool
        if winreg is None:
            raise AutostartError("winreg is unavailable", "AUTOSTART_UNSUPPORTED")
        try:
            with winreg.OpenKey(
                self.root, self.path, 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.DeleteValue(key, name)
                return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise AutostartError(
                "cannot delete %s\\%s: %s" % (self.path, name, exc)
            )


def default_command():
    # type: () -> str
    """Command line to register, or ``""`` outside a frozen build.

    ``sys.executable`` is the packaged ``DiskWarden.exe`` once frozen, which
    is exactly the file Explorer would start.  The development runner is not
    registered: a ``python.exe`` path from a throw-away virtualenv would
    break as soon as the environment moves and would surprise test runs.
    """
    override = (os.environ.get(ENV_COMMAND) or "").strip()
    if override:
        return override
    if not getattr(sys, "frozen", False):
        return ""
    exe = os.path.abspath(sys.executable)
    return '"%s" %s' % (exe, STARTUP_ARG)


class Autostart(object):
    """Read/write/remove the current user's DiskWarden startup value."""

    def __init__(
        self,
        command=None,
        registry=None,
        value_name=VALUE_NAME,
        environ=None,
    ):
        # type: (object, object, str, object) -> None
        self._environ = os.environ if environ is None else environ
        if command is None:
            self._command = default_command()
        elif callable(command):
            self._command = command() or ""
        else:
            self._command = str(command)
        self._registry = registry if registry is not None else WinRegistry()
        self.value_name = value_name

    @property
    def supported(self):
        # type: () -> bool
        return winreg is not None and bool(self._command)

    @property
    def command(self):
        # type: () -> str
        return self._command

    def registered(self):
        # type: () -> str
        """Raw value currently in the Run key (``""`` when absent)."""
        if winreg is None:
            return ""
        value = self._registry.get(self.value_name)
        return value if isinstance(value, str) else ""

    def is_enabled(self):
        # type: () -> bool
        """True only when this exact install is the registered command.

        A value left by an older extraction directory must not make the
        switch look enabled: that entry would launch a path the user has
        already deleted.
        """
        if not self.supported:
            return False
        return _same_command(self.registered(), self._command)

    def enable(self):
        # type: () -> str
        if winreg is None:
            raise AutostartError(
                "startup items are only available on Windows", "AUTOSTART_UNSUPPORTED"
            )
        if not self._command:
            raise AutostartError(
                "the current build has no launch command", "AUTOSTART_UNSUPPORTED"
            )
        self._registry.set(self.value_name, self._command)
        return self._command

    def disable(self):
        # type: () -> bool
        if winreg is None:
            return False
        return self._registry.delete(self.value_name)

    def status(self):
        # type: () -> dict
        payload = {
            "supported": self.supported,
            "enabled": False,
            "command": self._command,
            "registered": "",
            "error": "",
        }
        if not self.supported:
            return payload
        try:
            payload["registered"] = self.registered()
            payload["enabled"] = _same_command(
                payload["registered"], self._command
            )
        except AutostartError as exc:
            payload["error"] = str(exc)
        return payload

    def set_enabled(self, enabled):
        # type: (bool) -> dict
        if enabled:
            self.enable()
        else:
            self.disable()
        return self.status()


def _same_command(left, right):
    # type: (str, str) -> bool
    """Compare command lines ignoring case and surrounding whitespace."""
    if not left or not right:
        return False
    return " ".join(left.split()).lower() == " ".join(right.split()).lower()


__all__ = [
    "Autostart",
    "AutostartError",
    "RUN_KEY_PATH",
    "VALUE_NAME",
    "WinRegistry",
    "default_command",
]
