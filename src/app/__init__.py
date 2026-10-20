# -*- coding: utf-8 -*-
"""Host application layer (T12).

``src/app`` is the only place where the UI meets the engines:

* :mod:`src.app.events` -- bounded, monotonically sequenced event bus.
* :mod:`src.app.plans` -- in-memory plan registry with hash-bound confirm
  tokens and a five minute expiry (architecture §5.5).
* :mod:`src.app.jobs` -- one worker thread per long task, cancel tokens and
  the scan/write mutual exclusion.
* :mod:`src.app.service` -- the JSON-RPC method implementations.  It wires the
  scan, rules, migrate, rollback and store packages together and owns the
  migration journal/history glue that the engines deliberately leave to the
  caller.
* :mod:`src.app.bridge` -- schema validation and whitelist dispatch
  (architecture §5.1/§5.2).
* :mod:`src.app.main` -- pywebview host entry point.

Python 3.8 compatible (decision 0005).
"""

APP_VERSION = "0.9.0-beta.4"
PROTOCOL_VERSION = 1

__all__ = ["APP_VERSION", "PROTOCOL_VERSION"]
