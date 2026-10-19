# -*- coding: utf-8 -*-
"""Result cache for the third-layer AI analysis (T22).

The cache key is ``sha256(sanitized path, rules version, prompt version)`` —
exactly the tuple required by decision 0006.  A hit performs no network call.
Writes are atomic (temporary file in the same directory + ``os.replace``) so a
crash can never leave a half-written cache.

The cache is an optimisation only: unreadable/corrupt files are treated as
empty and write failures are logged and ignored, never propagated to the scan.

Python 3.8 compatible (decision 0005).
"""

import hashlib
import json
import logging
import os
import tempfile
import time
from typing import Any, Dict, Optional

CACHE_SCHEMA_VERSION = 1
DEFAULT_TTL_SECONDS = 30 * 24 * 60 * 60
DEFAULT_MAX_ENTRIES = 5000
VALID_SAFETIES = ("green", "red")

LOGGER_NAME = "disk_warden.ai"


def stable_file_version(path):
    # type: (str) -> str
    """Short content hash used as the rule version in cache keys."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    except (OSError, TypeError):
        return "unknown"
    return digest.hexdigest()[:16]


def cache_key(sanitized_path, rules_version, prompt_version):
    # type: (str, str, str) -> str
    """Stable 32-hex key; the raw path is never stored in the key itself."""
    raw = "%s\n%s\n%s" % (
        sanitized_path or "",
        rules_version or "unknown",
        prompt_version or "unknown",
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class AICache(object):
    """JSON-file cache with atomic writes and a bounded entry count."""

    def __init__(self, path, ttl_seconds=DEFAULT_TTL_SECONDS,
                 max_entries=DEFAULT_MAX_ENTRIES, clock=None, logger=None):
        # type: (str, int, int, object, Optional[logging.Logger]) -> None
        self.path = path
        self.ttl_seconds = max(1, int(ttl_seconds or DEFAULT_TTL_SECONDS))
        self.max_entries = max(1, int(max_entries or DEFAULT_MAX_ENTRIES))
        self._clock = clock if clock is not None else time.time
        self._logger = logger or logging.getLogger(LOGGER_NAME)
        self._entries = None  # type: Optional[Dict[str, dict]]

    def _load(self):
        # type: () -> Dict[str, dict]
        if self._entries is not None:
            return self._entries
        entries = {}  # type: Dict[str, dict]
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            raw_entries = data.get("entries") if isinstance(data, dict) else None
            if isinstance(raw_entries, dict):
                for key, value in raw_entries.items():
                    if isinstance(key, str) and isinstance(value, dict):
                        entries[key] = value
            elif raw_entries is not None:
                self._logger.warning("ai cache ignored: entries is not an object")
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            self._logger.warning("ai cache ignored: file is unreadable or corrupt")
        self._entries = entries
        return entries

    def _current_time(self):
        # type: () -> float
        return float(self._clock())

    def get(self, key):
        # type: (str) -> Optional[Dict[str, str]]
        """Return ``{"safety": ..., "reason": ...}`` or ``None`` on miss."""
        entries = self._load()
        entry = entries.get(key)
        if not isinstance(entry, dict):
            return None

        created = entry.get("created_at")
        try:
            created_at = float(created)
        except (TypeError, ValueError):
            entries.pop(key, None)
            return None

        if self._current_time() - created_at > self.ttl_seconds:
            entries.pop(key, None)
            return None

        safety = entry.get("safety")
        reason = entry.get("reason")
        if safety not in VALID_SAFETIES or not isinstance(reason, str):
            entries.pop(key, None)
            return None
        return {"safety": safety, "reason": reason}

    def set(self, key, safety, reason):
        # type: (str, str, str) -> None
        """Store one suggestion; failures are logged, never raised."""
        entries = self._load()
        entries[key] = {
            "safety": safety,
            "reason": reason,
            "created_at": self._current_time(),
        }
        self._prune(entries)
        self._flush(entries)

    def _prune(self, entries):
        # type: (Dict[str, dict]) -> None
        if len(entries) <= self.max_entries:
            return
        ordered = sorted(
            entries.items(),
            key=lambda pair: float(pair[1].get("created_at") or 0),
        )
        for key, _entry in ordered[: len(entries) - self.max_entries]:
            entries.pop(key, None)

    def _flush(self, entries):
        # type: (Dict[str, dict]) -> None
        directory = os.path.dirname(os.path.abspath(self.path))
        temp_path = None
        try:
            os.makedirs(directory, exist_ok=True)
            handle, temp_path = tempfile.mkstemp(
                prefix=".ai-cache-", suffix=".tmp", dir=directory
            )
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(
                    {
                        "schema_version": CACHE_SCHEMA_VERSION,
                        "entries": entries,
                    },
                    stream,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self.path)
            temp_path = None
        except OSError:
            self._logger.warning("ai cache write failed; continuing without it")
        finally:
            if temp_path is not None:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    def __len__(self):
        return len(self._load())


__all__ = [
    "AICache",
    "CACHE_SCHEMA_VERSION",
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_TTL_SECONDS",
    "cache_key",
    "stable_file_version",
]
