# -*- coding: utf-8 -*-
"""Tiny static file server rooted at the repository root.

``ui/index.html`` is served from ``/ui/index.html`` so its relative asset
paths (``assets/brand/app-icon-64.png``, the favicon) resolve exactly like
they do when the host loads the file from disk.
"""

import os
import threading

try:  # Python 3.7+
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
except ImportError:  # pragma: no cover - Python 2 is not supported anyway
    raise

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: A003 - stdlib signature
        pass


class StaticSite(object):
    """Context manager serving ``REPO_ROOT`` on an ephemeral port."""

    def __init__(self, root=None):
        self.root = root or REPO_ROOT
        self.httpd = None
        self.thread = None
        self.port = None

    @property
    def base_url(self):
        return "http://127.0.0.1:%d" % self.port

    def start(self):
        handler = lambda *a, **kw: _QuietHandler(*a, directory=self.root, **kw)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever)
        self.thread.daemon = True
        self.thread.start()
        return self

    def stop(self):
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        if self.thread is not None:
            self.thread.join(2)
            self.thread = None

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        return False


if __name__ == "__main__":  # manual smoke test
    site = StaticSite().start()
    print(site.base_url + "/ui/index.html")
    try:
        site.thread.join()
    except KeyboardInterrupt:
        site.stop()
