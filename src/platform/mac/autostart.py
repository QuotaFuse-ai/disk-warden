# -*- coding: utf-8 -*-
"""macOS startup item: a per-user LaunchAgent (``~/Library/LaunchAgents``).

No administrator is needed and nothing global is touched, matching the
Windows ``HKCU\\...\\Run`` adapter.  The agent starts the packaged ``.app``
through ``open`` with ``--tray``, so LaunchServices owns the process and Disk
Warden comes up in the menu bar without a window.

The plist is written with :mod:`plistlib` (no ``launchctl`` call), so the
switch is deterministic and testable off-macOS.  Like the Windows Run key, it
takes effect at the next login; the UI says so.

Python 3.8 compatible (decision 0005).
"""

import os
import plistlib
import posixpath
import shlex
import sys
import tempfile

LABEL = "com.quotafuse.diskwarden"
PLIST_NAME = LABEL + ".plist"
STARTUP_ARG = "--tray"
OPEN_COMMAND = "/usr/bin/open"
VALUE_NAME = "DiskWarden"
ENV_COMMAND = "DISKWARDEN_AUTOSTART_COMMAND"
LAUNCH_AGENTS_DIR = "Library/LaunchAgents"


class AutostartError(Exception):
    """The startup item could not be read or written."""

    def __init__(self, message, code="AUTOSTART_FAILED"):
        # type: (str, str) -> None
        Exception.__init__(self, message)
        self.code = code


def default_plist_path(environ=None):
    # type: (object) -> str
    """``~/Library/LaunchAgents/com.quotafuse.diskwarden.plist``."""
    environ = os.environ if environ is None else environ
    home = environ.get("HOME") or os.path.expanduser("~")
    # POSIX join: the plist path is a macOS path even when tests run on
    # Windows, and ``/`` is accepted by ``open`` on every host.
    return "/".join((home.rstrip("/"), LAUNCH_AGENTS_DIR, PLIST_NAME))


def app_bundle_for(executable):
    # type: (str) -> str
    """``.../DiskWarden.app`` for a binary inside ``Contents/MacOS``, else ``""``.

    Every path handled here is a macOS path, so it is parsed with
    :mod:`posixpath`: on a Windows test host ``os.path`` would rewrite
    ``/Applications/...`` into ``C:\\Applications\\...`` and never see the
    bundle.
    """
    macos = posixpath.dirname(posixpath.abspath(executable or ""))
    contents = posixpath.dirname(macos)
    bundle = posixpath.dirname(contents)
    if posixpath.basename(macos) != "MacOS":
        return ""
    if posixpath.basename(contents) != "Contents":
        return ""
    if not bundle.endswith(".app"):
        return ""
    return bundle


def default_command():
    # type: () -> str
    """Quoted command line to register, or ``""`` outside a frozen build.

    Inside a ``.app`` the agent goes through ``open`` so LaunchServices starts
    the bundle itself: a login item that executes ``Contents/MacOS/DiskWarden``
    directly runs the same code but outside its bundle, which loses the app
    identity (icon, Dock entry, activation) macOS uses for a GUI process.
    """
    override = (os.environ.get(ENV_COMMAND) or "").strip()
    if override:
        return override
    if not getattr(sys, "frozen", False):
        return ""
    exe = posixpath.abspath(sys.executable)
    bundle = app_bundle_for(exe)
    if bundle:
        return "%s -n %s --args %s" % (OPEN_COMMAND, _quote(bundle), STARTUP_ARG)
    return '"%s" %s' % (exe, STARTUP_ARG)


def _quote(argument):
    # type: (str) -> str
    if not argument or any(character in argument for character in ' \t"'):
        return '"%s"' % argument.replace('"', '\\"')
    return argument


def _command_from_arguments(arguments):
    # type: (list) -> str
    return " ".join(_quote(str(argument)) for argument in arguments)


def _arguments_from_command(command):
    # type: (str) -> list
    try:
        return [str(part) for part in shlex.split(command)]
    except ValueError:
        return [command] if command else []


class Autostart(object):
    """Read/write/remove the current user's DiskWarden LaunchAgent."""

    def __init__(
        self,
        command=None,
        registry=None,
        value_name=VALUE_NAME,
        environ=None,
        plist_path=None,
    ):
        # type: (object, object, str, object, str) -> None
        self._environ = os.environ if environ is None else environ
        if command is None:
            self._command = default_command()
        elif callable(command):
            self._command = command() or ""
        else:
            self._command = str(command)
        self._arguments = _arguments_from_command(self._command)
        self.value_name = value_name
        self.plist_path = (
            plist_path
            if plist_path is not None
            else default_plist_path(self._environ)
        )

    @property
    def supported(self):
        # type: () -> bool
        return bool(self._arguments)

    @property
    def command(self):
        # type: () -> str
        return self._command

    @property
    def arguments(self):
        # type: () -> list
        return list(self._arguments)

    def registered(self):
        # type: () -> str
        """Command line currently stored in the LaunchAgent (``""`` if none)."""
        arguments = self._read_arguments()
        if not arguments:
            return ""
        return _command_from_arguments(arguments)

    def is_enabled(self):
        # type: () -> bool
        if not self.supported:
            return False
        return self._read_arguments() == self._arguments

    def enable(self):
        # type: () -> str
        if not self._arguments:
            raise AutostartError(
                "the current build has no launch command", "AUTOSTART_UNSUPPORTED"
            )
        payload = {
            "Label": LABEL,
            "ProgramArguments": self._arguments,
            "RunAtLoad": True,
            # A crash must not turn into a restart loop with two tray icons.
            "KeepAlive": False,
            "ProcessType": "Interactive",
        }
        directory = os.path.dirname(self.plist_path)
        try:
            if directory and not os.path.isdir(directory):
                os.makedirs(directory, exist_ok=True)
            data = plistlib.dumps(payload, fmt=plistlib.FMT_XML)
            handle, temporary = tempfile.mkstemp(
                prefix=".diskwarden-", suffix=".plist", dir=directory or None
            )
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(data)
                os.chmod(temporary, 0o644)
                os.replace(temporary, self.plist_path)
            except BaseException:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise
        except (OSError, ValueError) as exc:
            raise AutostartError(
                "cannot write %s: %s" % (self.plist_path, exc)
            )
        return self._command

    def disable(self):
        # type: () -> bool
        try:
            os.unlink(self.plist_path)
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise AutostartError(
                "cannot remove %s: %s" % (self.plist_path, exc)
            )

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
            payload["enabled"] = self._read_arguments() == self._arguments
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

    def _read_arguments(self):
        # type: () -> list
        """ProgramArguments from the plist, or ``[]`` when absent/invalid."""
        try:
            with open(self.plist_path, "rb") as stream:
                payload = plistlib.load(stream)
        except FileNotFoundError:
            return []
        except (OSError, ValueError, plistlib.InvalidFileException) as exc:
            if isinstance(exc, OSError):
                raise AutostartError(
                    "cannot read %s: %s" % (self.plist_path, exc)
                )
            return []
        arguments = payload.get("ProgramArguments") if isinstance(payload, dict) else None
        if not isinstance(arguments, list):
            return []
        return [str(argument) for argument in arguments]


__all__ = [
    "Autostart",
    "AutostartError",
    "ENV_COMMAND",
    "LABEL",
    "LAUNCH_AGENTS_DIR",
    "OPEN_COMMAND",
    "PLIST_NAME",
    "STARTUP_ARG",
    "app_bundle_for",
    "default_command",
    "default_plist_path",
]
