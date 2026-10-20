# -*- coding: utf-8 -*-
"""Open a directory in the operating system file manager.

The page cannot start Explorer from the WebView2 sandbox, so the "open
folder" buttons go through the bridge into this module.  Paths arrive from
the page; ``%APPDATA%``-style variables are expanded here so both the
prototype catalog and real scan results resolve to a real directory.

Python 3.8 compatible (decision 0005).
"""

import os
import re
import subprocess
import sys

_ENV_PATTERN = re.compile(r"%([^%]+)%")


class RevealError(OSError):
    """The shell could not be asked to show the directory."""


def expand_path(path):
    # type: (str) -> str
    """Expand ``%VAR%``/``$VAR``/``~`` and return an absolute, normalised path."""
    text = str(path)

    def _replace(match):
        return os.environ.get(match.group(1), match.group(0))

    text = _ENV_PATTERN.sub(_replace, text)
    text = os.path.expanduser(os.path.expandvars(text))
    return os.path.abspath(os.path.normpath(text))


def reveal_directory(path):
    # type: (str) -> str
    """Open ``path`` in Explorer/Finder/desktop and return the opened path.

    Raises ``FileNotFoundError`` when the directory does not exist and
    ``RevealError`` when the shell refuses to start.
    """
    target = expand_path(path)
    if not os.path.isdir(target):
        raise FileNotFoundError(2, "directory does not exist", target)
    _open_directory(target)
    return target


def _open_directory(target):
    # type: (str) -> None
    if sys.platform.startswith("win"):
        os.startfile(target)
    elif sys.platform == "darwin":
        _spawn(["open", target])
    else:
        _spawn(["xdg-open", target])


def _spawn(argv):
    # type: (list) -> None
    try:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as exc:
        raise RevealError(
            exc.errno, "cannot start %s: %s" % (argv[0], exc), argv[0]
        )


__all__ = ["RevealError", "expand_path", "reveal_directory"]
