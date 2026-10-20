# -*- coding: utf-8 -*-
"""macOS version detection and the unsigned-app first-launch guide (T21).

This build ships as an unsigned ``.app`` inside a zip, so Gatekeeper stops the
very first launch.  What the user has to click depends on the macOS version:

* macOS 15 (Sequoia) and newer removed the right-click bypass.  The app has to
  be allowed in *System Settings -> Privacy & Security -> Open Anyway*.
* Big Sur (11) through Sonoma (14) still accept *Control-click -> Open*.

Only this module knows that difference.  :func:`first_launch_guide` returns
plain data (tier, step ids, the advanced command), so the UI renders whatever
it is given and neither the business layer nor the page branches on the OS.

Python 3.8 compatible (decision 0005).
"""

import platform
import posixpath
import subprocess
import sys

#: First release that no longer offers the right-click bypass.
SETTINGS_PANE_MAJOR = 15

#: ``LSMinimumSystemVersion`` in ``packaging/DiskWarden-macos.spec``.
MIN_SUPPORTED_MAJOR = 11

TIER_SETTINGS_PANE = "settings_pane"
TIER_RIGHT_CLICK = "right_click"
TIER_UNKNOWN = "unknown"

#: Ordered step ids; the page owns the wording for each one (i18n).
STEPS = {
    TIER_SETTINGS_PANE: (
        "move_to_applications",
        "try_to_open",
        "dismiss_block",
        "open_privacy_security",
        "allow_anyway",
    ),
    TIER_RIGHT_CLICK: (
        "move_to_applications",
        "control_click_open",
        "confirm_open",
        "double_click_later",
    ),
}

FALLBACK_BUNDLE = "/Applications/Disk Warden.app"


def parse_version(text):
    # type: (object) -> tuple
    """``"15.1.1"`` -> ``(15, 1, 1)``; anything unusable -> ``(0, 0, 0)``.

    A trailing build suffix (``"15.1 (24B83)"``) is ignored rather than
    rejected, so a hand-edited ``sw_vers`` output still parses.
    """
    numbers = []
    for part in str(text or "").strip().split(".")[:3]:
        digits = ""
        for character in part.strip():
            if not character.isdigit():
                break
            digits += character
        if not digits:
            break
        numbers.append(int(digits))
    while len(numbers) < 3:
        numbers.append(0)
    return tuple(numbers)


def _sw_vers():
    # type: () -> str
    """``sw_vers -productVersion``, the fallback when plist parsing fails."""
    if sys.platform != "darwin":
        return ""
    try:
        completed = subprocess.run(
            ["/usr/bin/sw_vers", "-productVersion"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError:
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.decode("utf-8", "replace").strip()


def macos_version():
    # type: () -> tuple
    """``(major, minor, patch)`` of this Mac, ``(0, 0, 0)`` when unknown.

    ``platform.mac_ver()`` reads ``SystemVersion.plist`` through
    :mod:`plistlib`, which covers both the XML and the binary format; the
    ``sw_vers`` call is only a fallback.
    """
    text = ""
    try:
        text = platform.mac_ver()[0]
    except Exception:  # pragma: no cover - defensive, plist parse failures
        text = ""
    if not text:
        text = _sw_vers()
    return parse_version(text)


def version_text(version):
    # type: (tuple) -> str
    """``(15, 1, 0)`` -> ``"15.1"``; an unknown version -> ``""``."""
    major, minor, patch = version
    if not major:
        return ""
    if patch:
        return "%d.%d.%d" % (major, minor, patch)
    return "%d.%d" % (major, minor)


def bundle_path():
    # type: () -> str
    """The running ``.app``, or the conventional install path as a fallback."""
    from .autostart import app_bundle_for

    return app_bundle_for(getattr(sys, "executable", "")) or FALLBACK_BUNDLE


def _shell_quote(text):
    # type: (str) -> str
    return "'" + text.replace("'", "'\\''") + "'"


def quarantine_command(bundle=None):
    # type: (object) -> str
    """The ``xattr`` one-liner, quoted for the path it actually needs.

    This is the advanced escape hatch, not the recommended path: it removes
    the quarantine flag from the whole bundle and hides the fact that the
    build is unsigned.
    """
    return "xattr -dr com.apple.quarantine %s" % _shell_quote(
        bundle or bundle_path()
    )


def _normalize(version):
    # type: (object) -> tuple
    if version is None:
        return macos_version()
    if isinstance(version, (tuple, list)):
        values = [int(item) for item in list(version)[:3]]
        while len(values) < 3:
            values.append(0)
        return tuple(values)
    return parse_version(version)


def first_launch_guide(first_run_done=False, version=None):
    # type: (bool, object) -> dict
    """Everything the first-launch dialog needs, as plain JSON-safe data.

    ``show`` lives here so the page never has to know which platform it is
    drawing: the Windows build returns ``show: False`` from the dispatcher in
    :mod:`src.platform`, and an acknowledged guide stays away until the user
    opens it again from the settings dialog.
    """
    detected = _normalize(version)
    major = detected[0]
    if major >= SETTINGS_PANE_MAJOR:
        tier = TIER_SETTINGS_PANE
    elif major >= 1:
        tier = TIER_RIGHT_CLICK
    else:
        tier = TIER_UNKNOWN
    return {
        "show": not bool(first_run_done),
        "platform": "darwin",
        # An unknown version keeps the macOS 15 path plus the older fallback
        # mentioned by ``firstRun.versionUnknown``; guessing silently would be
        # worse than saying so.
        "version": version_text(detected),
        "major": major,
        "tier": tier,
        "steps": list(STEPS.get(tier, STEPS[TIER_SETTINGS_PANE])),
        "advanced_command": quarantine_command(),
        "min_supported": MIN_SUPPORTED_MAJOR,
        "supported": major == 0 or major >= MIN_SUPPORTED_MAJOR,
    }


__all__ = [
    "FALLBACK_BUNDLE",
    "MIN_SUPPORTED_MAJOR",
    "SETTINGS_PANE_MAJOR",
    "STEPS",
    "TIER_RIGHT_CLICK",
    "TIER_SETTINGS_PANE",
    "TIER_UNKNOWN",
    "bundle_path",
    "first_launch_guide",
    "macos_version",
    "parse_version",
    "quarantine_command",
    "version_text",
]
