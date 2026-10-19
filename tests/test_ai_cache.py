# -*- coding: utf-8 -*-
"""Cache tests for the AI layer (T22).

The cache is an optimisation: hits avoid requests, writes are atomic, and a
corrupt or stale file never breaks the scan.
"""

import json
import logging
import os
import tempfile
import unittest

from src.core.ai.cache import (
    CACHE_SCHEMA_VERSION,
    AICache,
    cache_key,
    stable_file_version,
)

LOGGER = logging.getLogger("test_ai_cache")
LOGGER.addHandler(logging.NullHandler())
LOGGER.propagate = False


class Clock(object):
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class CacheKeyTests(unittest.TestCase):
    def test_key_is_stable_and_short(self):
        key = cache_key("Cursor/Cache", "rules-v1", "1")
        self.assertEqual(len(key), 32)
        self.assertEqual(key, cache_key("Cursor/Cache", "rules-v1", "1"))
        self.assertNotEqual(key, cache_key("Cursor/Logs", "rules-v1", "1"))

    def test_rules_and_prompt_versions_change_the_key(self):
        base = cache_key("Cursor/Cache", "rules-v1", "1")
        self.assertNotEqual(base, cache_key("Cursor/Cache", "rules-v2", "1"))
        self.assertNotEqual(base, cache_key("Cursor/Cache", "rules-v1", "2"))


class RoundTripTests(unittest.TestCase):
    def test_set_then_get(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "ai-cache.json")
            cache = AICache(path, logger=LOGGER)
            self.assertIsNone(cache.get("missing"))
            cache.set("key", "green", "regenerable cache")
            self.assertEqual(
                cache.get("key"),
                {"safety": "green", "reason": "regenerable cache"},
            )

    def test_file_is_atomic_and_well_formed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "ai-cache.json")
            cache = AICache(path, logger=LOGGER)
            cache.set("key", "red", "credentials")
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            self.assertEqual(data["schema_version"], CACHE_SCHEMA_VERSION)
            self.assertIn("key", data["entries"])
            leftovers = [
                name for name in os.listdir(directory)
                if name.startswith(".ai-cache-")
            ]
            self.assertEqual(leftovers, [])

    def test_persisted_cache_is_reloaded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "ai-cache.json")
            AICache(path, logger=LOGGER).set("key", "green", "cache")
            second = AICache(path, logger=LOGGER)
            self.assertEqual(second.get("key")["safety"], "green")


class ExpiryTests(unittest.TestCase):
    def test_entry_expires_after_ttl(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "ai-cache.json")
            clock = Clock()
            cache = AICache(path, ttl_seconds=10, clock=clock, logger=LOGGER)
            cache.set("key", "green", "cache")

            clock.now += 5
            self.assertIsNotNone(cache.get("key"))
            clock.now += 6
            self.assertIsNone(cache.get("key"))
            self.assertEqual(len(cache), 0)

    def test_entries_are_pruned_to_the_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "ai-cache.json")
            clock = Clock()
            cache = AICache(path, max_entries=2, clock=clock, logger=LOGGER)
            for index in range(3):
                cache.set("key-%d" % index, "green", "cache")
                clock.now += 1

            self.assertEqual(len(cache), 2)
            self.assertNotIn("key-0", cache._entries)
            self.assertIn("key-1", cache._entries)
            self.assertIn("key-2", cache._entries)


class RobustnessTests(unittest.TestCase):
    def test_corrupt_file_is_treated_as_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "ai-cache.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            cache = AICache(path, logger=LOGGER)
            self.assertEqual(len(cache), 0)
            cache.set("key", "green", "cache")
            self.assertEqual(cache.get("key")["safety"], "green")

    def test_invalid_entry_values_are_dropped(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "ai-cache.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "schema_version": CACHE_SCHEMA_VERSION,
                        "entries": {
                            "bad-safety": {
                                "safety": "purple",
                                "reason": "?",
                                "created_at": 1000,
                            },
                            "bad-time": {
                                "safety": "green",
                                "reason": "cache",
                                "created_at": "yesterday",
                            },
                        },
                    },
                    handle,
                )
            cache = AICache(path, clock=Clock(1001), logger=LOGGER)
            self.assertIsNone(cache.get("bad-safety"))
            self.assertIsNone(cache.get("bad-time"))

    def test_unwritable_path_does_not_raise(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "missing-parent", "cache.json")
            cache = AICache(path, logger=LOGGER)
            cache.set("key", "green", "cache")  # creates the directory
            self.assertTrue(os.path.exists(path))


class FileVersionTests(unittest.TestCase):
    def test_content_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "rules.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{}")
            first = stable_file_version(path)
            self.assertEqual(len(first), 16)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"x":1}')
            self.assertNotEqual(first, stable_file_version(path))

    def test_missing_file_is_unknown(self):
        self.assertEqual(stable_file_version("does-not-exist.json"), "unknown")


if __name__ == "__main__":
    unittest.main()
