# -*- coding: utf-8 -*-
"""Daily JSONL operation log with 14-day rotation (architecture 7).

One file per local day, ``logs/<yyyy-MM-dd>.log``.  Lines are appended and
flushed immediately so a crash loses at most the line being written; the log
is diagnostic data, never a source of truth.  Writing a log line is therefore
allowed to fail without aborting a safe operation, while callers that are
about to mutate data can ask :meth:`JsonlLogger.is_available` first.

Only whitelisted fields are written.  Keys that look like credentials and
free-form nested payloads are dropped, so a caller cannot accidentally log a
token or file content (architecture 7).  :func:`mask_path` and
:func:`mask_text` implement the diagnostic default of hiding full user paths.

Python 3.8 compatible (decision 0005).
"""

import datetime
import json
import os
import re

from .atomic import StoreError, utc_now

DEFAULT_RETENTION_DAYS = 14
LOG_SUFFIX = ".log"
DAY_FORMAT = "%Y-%m-%d"
LOG_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.log$")
MAX_STRING_LENGTH = 1024
HOME_PLACEHOLDER = "<home>"
ROOT_PLACEHOLDER = "<data_root>"

#: Fields a log record may carry.  Anything else is dropped on purpose.
SAFE_FIELDS = (
    "level",
    "event",
    "job_id",
    "type",
    "tool_id",
    "item_id",
    "stage",
    "step",
    "source",
    "target",
    "path",
    "state",
    "bytes",
    "files",
    "result",
    "error",
    "error_code",
    "exit_code",
    "duration_seconds",
)

PATH_FIELDS = frozenset(("source", "target", "path"))


def _casefold_replacement(value, pattern, replacement):
    # type: (str, str, str) -> str
    flags = re.IGNORECASE if os.name == "nt" else 0
    return re.sub(re.escape(pattern), replacement, value, flags=flags)


def mask_path(path, roots=()):
    # type: (object, object) -> str
    """Replace the user home and known roots with placeholders (diagnostics)."""
    if path is None:
        return ""
    value = str(path)
    for root in roots or ():
        if not root:
            continue
        value = _casefold_replacement(value, str(root), ROOT_PLACEHOLDER)
    home = os.path.expanduser("~")
    if home and home not in ("~", os.sep):
        value = _casefold_replacement(value, home, HOME_PLACEHOLDER)
    return value


def mask_text(text, roots=()):
    # type: (object, object) -> str
    """Mask paths inside a free-form diagnostics string."""
    return mask_path(text, roots)


def _safe_scalar(key, value, mask_paths, roots):
    # type: (str, object, bool, object) -> object
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value, 3)
    if isinstance(value, (bytes, bytearray)):
        return "<bytes:%d>" % len(value)
    if isinstance(value, str):
        text = value
        if mask_paths:
            text = mask_path(text, roots)
        if len(text) > MAX_STRING_LENGTH:
            text = text[:MAX_STRING_LENGTH] + "..."
        return text
    # Containers may carry file content or tokens; never serialise them.
    return "<%s>" % type(value).__name__


class JsonlLogger(object):
    """Append-only daily JSONL log below ``<data_root>/logs``."""

    def __init__(self, logs_dir, retention_days=DEFAULT_RETENTION_DAYS,
                 mask_paths=False, roots=()):
        # type: (str, int, bool, object) -> None
        if not logs_dir:
            raise StoreError("logs directory is required")
        self.logs_dir = os.path.abspath(os.path.normpath(logs_dir))
        self.retention_days = max(1, int(retention_days))
        self.mask_paths = bool(mask_paths)
        self.roots = tuple(root for root in (roots or ()) if root)
        self.last_error = None  # type: object
        self.last_written_path = None  # type: object

    def day_for(self, moment=None):
        # type: (object) -> str
        value = moment or datetime.datetime.now()
        if isinstance(value, datetime.datetime):
            return value.strftime(DAY_FORMAT)
        return str(value)[:10]

    def path_for(self, day=None):
        # type: (object) -> str
        return os.path.join(self.logs_dir, "%s%s" % (self.day_for(day), LOG_SUFFIX))

    def log(self, event, now=None, **fields):
        # type: (str, object, object) -> bool
        """Append one record.  Returns False instead of raising on I/O errors."""
        record = {"time": utc_now(now), "event": str(event)}
        for key in SAFE_FIELDS:
            if key in fields:
                record[key] = _safe_scalar(
                    key, fields[key], self.mask_paths, self.roots
                )
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        path = self.path_for(now)
        try:
            os.makedirs(self.logs_dir, exist_ok=True)
            with open(path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
                handle.write("\n")
                handle.flush()
        except OSError as exc:
            self.last_error = str(exc) or exc.__class__.__name__
            return False
        self.last_error = None
        self.last_written_path = path
        return True

    def is_available(self):
        # type: () -> bool
        """Probe the log directory without relying on a successful write."""
        probe = os.path.join(
            self.logs_dir, ".dw-log-probe-%d" % os.getpid()
        )
        try:
            os.makedirs(self.logs_dir, exist_ok=True)
            with open(probe, "w", encoding="utf-8") as handle:
                handle.write("ok")
            os.unlink(probe)
            return True
        except OSError:
            return False

    def rotate(self, now=None):
        # type: (object) -> list
        """Delete daily files older than ``retention_days``; never raises."""
        cutoff = (now or datetime.datetime.now()).date() - datetime.timedelta(
            days=self.retention_days
        )
        removed = []  # type: list
        try:
            names = os.listdir(self.logs_dir)
        except OSError:
            return removed
        for name in names:
            match = LOG_NAME_RE.match(name)
            if not match:
                continue
            try:
                day = datetime.datetime.strptime(
                    match.group(1), DAY_FORMAT
                ).date()
            except ValueError:
                continue
            if day >= cutoff:
                continue
            try:
                os.unlink(os.path.join(self.logs_dir, name))
                removed.append(name)
            except OSError:
                continue
        return removed

    def read(self, day=None, limit=200):
        # type: (object, int) -> list
        """Return parsed records from one day, newest last; bad lines ignored."""
        path = self.path_for(day)
        records = []  # type: list
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(payload, dict):
                        records.append(payload)
        except OSError:
            return []
        if limit and limit > 0:
            return records[-limit:]
        return records


__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "HOME_PLACEHOLDER",
    "JsonlLogger",
    "LOG_NAME_RE",
    "MAX_STRING_LENGTH",
    "PATH_FIELDS",
    "ROOT_PLACEHOLDER",
    "SAFE_FIELDS",
    "mask_path",
    "mask_text",
]
