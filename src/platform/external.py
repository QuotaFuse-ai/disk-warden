# -*- coding: utf-8 -*-
"""Open an external URL in the user's default browser.

The WebView2 sandbox cannot hand a link to the system browser, so the
feedback "open issue" button goes through the bridge into this module.  The
business layer validates the URL against its whitelist before calling here.

Python 3.8 compatible (decision 0005).
"""

import webbrowser


class ExternalOpenError(OSError):
    """The default browser could not be started."""


def open_url(url):
    # type: (str) -> None
    """Ask the system to open ``url``; raise :class:`ExternalOpenError`."""
    try:
        opened = webbrowser.open(url, new=2, autoraise=True)
    except Exception as exc:  # webbrowser backends may raise anything
        raise ExternalOpenError("cannot start the browser: %s" % exc)
    if not opened:
        raise ExternalOpenError("no browser accepted the URL")


__all__ = ["ExternalOpenError", "open_url"]
