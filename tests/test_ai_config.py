# -*- coding: utf-8 -*-
"""Configuration tests for the AI layer (T22).

The layer must be off by default and only become available with an explicit
switch, a key, an endpoint and a model.  The key never appears in repr or in
the printable public view.
"""

import os
import unittest

from src.core.ai.config import (
    AIConfig,
    DEFAULT_CACHE_TTL_SECONDS,
    DEFAULT_MAX_CALLS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_MAX_PATH_SEGMENTS,
    DEFAULT_MAX_TOTAL_TOKENS,
    DEFAULT_TIMEOUT_SECONDS,
    REASON_DISABLED,
    REASON_MISSING_BASE_URL,
    REASON_MISSING_KEY,
    REASON_MISSING_MODEL,
    default_cache_path,
    load_config,
)

FULL_ENV = {
    "LOCALAPPDATA": r"C:\Users\alice\AppData\Local",
    "DISK_WARDEN_AI_ENABLED": "1",
    "DISK_WARDEN_AI_BASE_URL": "https://api.example.test/v1",
    "DISK_WARDEN_AI_MODEL": "test-model",
    "DISK_WARDEN_AI_API_KEY": "sk-test-secret",
}


class DefaultsTests(unittest.TestCase):
    def test_off_by_default(self):
        config = load_config(environ={})
        self.assertFalse(config.enabled)
        self.assertFalse(config.available)
        self.assertEqual(config.disabled_reason, REASON_DISABLED)

    def test_default_budgets(self):
        config = load_config(environ=FULL_ENV)
        self.assertEqual(config.max_calls, DEFAULT_MAX_CALLS)
        self.assertEqual(config.max_total_tokens, DEFAULT_MAX_TOTAL_TOKENS)
        self.assertEqual(config.max_output_tokens, DEFAULT_MAX_OUTPUT_TOKENS)
        self.assertEqual(config.timeout_seconds, DEFAULT_TIMEOUT_SECONDS)
        self.assertEqual(config.ttl_seconds, DEFAULT_CACHE_TTL_SECONDS)
        self.assertEqual(config.max_path_segments, DEFAULT_MAX_PATH_SEGMENTS)


class AvailabilityTests(unittest.TestCase):
    def test_enabled_by_environment_switch(self):
        config = load_config(environ=FULL_ENV)
        self.assertTrue(config.enabled)
        self.assertTrue(config.available)
        self.assertEqual(config.disabled_reason, "")

    def test_missing_key(self):
        env = dict(FULL_ENV)
        del env["DISK_WARDEN_AI_API_KEY"]
        config = load_config(environ=env)
        self.assertTrue(config.enabled)
        self.assertFalse(config.available)
        self.assertEqual(config.disabled_reason, REASON_MISSING_KEY)

    def test_missing_base_url(self):
        env = dict(FULL_ENV)
        del env["DISK_WARDEN_AI_BASE_URL"]
        config = load_config(environ=env)
        self.assertEqual(config.disabled_reason, REASON_MISSING_BASE_URL)

    def test_missing_model(self):
        env = dict(FULL_ENV)
        del env["DISK_WARDEN_AI_MODEL"]
        config = load_config(environ=env)
        self.assertEqual(config.disabled_reason, REASON_MISSING_MODEL)

    def test_explicit_switch_overrides_environment(self):
        self.assertFalse(load_config(environ=FULL_ENV, enabled=False).enabled)
        self.assertTrue(load_config(environ={}, enabled=True).enabled)

    def test_settings_are_used_as_fallback(self):
        config = load_config(
            environ={},
            settings={
                "enabled": True,
                "base_url": "https://settings.example/v1",
                "model": "settings-model",
                "api_key": "sk-settings",
            },
        )
        self.assertTrue(config.available)
        self.assertEqual(config.model, "settings-model")

    def test_environment_wins_over_settings(self):
        config = load_config(
            environ={"DISK_WARDEN_AI_MODEL": "env-model"},
            settings={"model": "settings-model"},
            enabled=True,
        )
        self.assertEqual(config.model, "env-model")


class BudgetParsingTests(unittest.TestCase):
    def _config(self, **env):
        values = dict(FULL_ENV)
        values.update(env)
        return load_config(environ=values)

    def test_numeric_environment_values_are_parsed(self):
        config = self._config(
            DISK_WARDEN_AI_MAX_CALLS="5",
            DISK_WARDEN_AI_MAX_TOKENS="1000",
            DISK_WARDEN_AI_MAX_OUTPUT_TOKENS="64",
            DISK_WARDEN_AI_TIMEOUT="2.5",
            DISK_WARDEN_AI_CACHE_TTL="60",
            DISK_WARDEN_AI_MAX_PATH_SEGMENTS="4",
        )
        self.assertEqual(config.max_calls, 5)
        self.assertEqual(config.max_total_tokens, 1000)
        self.assertEqual(config.max_output_tokens, 64)
        self.assertEqual(config.timeout_seconds, 2.5)
        self.assertEqual(config.ttl_seconds, 60)
        self.assertEqual(config.max_path_segments, 4)

    def test_values_are_clamped(self):
        config = self._config(
            DISK_WARDEN_AI_MAX_CALLS="99999",
            DISK_WARDEN_AI_MAX_OUTPUT_TOKENS="0",
            DISK_WARDEN_AI_TIMEOUT="0",
            DISK_WARDEN_AI_MAX_PATH_SEGMENTS="99",
        )
        self.assertEqual(config.max_calls, 10000)
        self.assertEqual(config.max_output_tokens, 1)
        self.assertEqual(config.timeout_seconds, 0.1)
        self.assertEqual(config.max_path_segments, 6)

    def test_garbage_falls_back_to_defaults(self):
        config = self._config(
            DISK_WARDEN_AI_MAX_CALLS="many",
            DISK_WARDEN_AI_MAX_OUTPUT_TOKENS="lots",
            DISK_WARDEN_AI_TIMEOUT="soon",
        )
        self.assertEqual(config.max_calls, DEFAULT_MAX_CALLS)
        self.assertEqual(config.max_output_tokens, DEFAULT_MAX_OUTPUT_TOKENS)
        self.assertEqual(config.timeout_seconds, DEFAULT_TIMEOUT_SECONDS)


class CachePathTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "LOCALAPPDATA is the Windows cache base")
    def test_default_cache_path_uses_local_app_data(self):
        environ = {
            "LOCALAPPDATA": r"C:\Users\alice\AppData\Local",
        }
        self.assertEqual(
            default_cache_path(environ).replace("\\", "/"),
            "C:/Users/alice/AppData/Local/disk-warden/ai-cache.json",
        )

    @unittest.skipIf(os.name == "nt", "POSIX hosts use XDG_CACHE_HOME / HOME")
    def test_default_cache_path_uses_the_posix_cache_base(self):
        self.assertEqual(
            default_cache_path({"XDG_CACHE_HOME": "/home/alice/.cache"}),
            os.path.join("/home/alice/.cache", "disk-warden", "ai-cache.json"),
        )
        self.assertEqual(
            default_cache_path({"HOME": "/home/alice"}),
            os.path.join("/home/alice", ".cache", "disk-warden", "ai-cache.json"),
        )

    def test_cache_path_is_only_filled_when_enabled(self):
        self.assertEqual(load_config(environ={}).cache_path, "")
        enabled = load_config(environ=FULL_ENV)
        if os.name == "nt":
            self.assertTrue(enabled.cache_path.endswith(
                os.path.join("disk-warden", "ai-cache.json")
            ))

    def test_cache_path_override(self):
        env = dict(FULL_ENV)
        env["DISK_WARDEN_AI_CACHE"] = "/tmp/ai-cache.json"
        self.assertEqual(load_config(environ=env).cache_path, "/tmp/ai-cache.json")


class SecretHandlingTests(unittest.TestCase):
    def test_key_is_absent_from_repr_and_public_dict(self):
        config = AIConfig(enabled=True, api_key="sk-test-secret")
        self.assertNotIn("sk-test-secret", repr(config))
        public = config.public_dict()
        self.assertNotIn("sk-test-secret", repr(public))
        self.assertNotIn("api_key", public)

    def test_public_dict_reports_availability(self):
        environ = dict(FULL_ENV)
        if os.name != "nt":
            # The cache base is per-platform; without one the layer still
            # works but reports caching as disabled.
            environ["XDG_CACHE_HOME"] = "/home/alice/.cache"
        public = load_config(environ=environ).public_dict()
        self.assertTrue(public["available"])
        self.assertEqual(public["model"], "test-model")
        self.assertTrue(public["cache_enabled"])


if __name__ == "__main__":
    unittest.main()
