"""UI tour: screenshot every page of ui/index.html without the packaged host.

The SPA talks to the host through ``window.pywebview.api.rpc``.  That object is
read at call time, so injecting a fake bridge before the document scripts run
lets the whole UI render inside plain Chromium -- no PyInstaller build, no
Windows, no pywebview, nothing installed for end users.

See ``python -m tools.uitour --help`` (or the module docstring in
``__main__.py``) for usage.
"""

__all__ = ["fixtures", "fake_bridge", "server", "tour", "gallery"]

VERSION = "1.0"
