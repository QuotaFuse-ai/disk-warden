# -*- coding: utf-8 -*-
"""Auth state and token persistence (T16).

Two rules are tested here because they are acceptance items:

* the state file never holds a token -- only flags and a public profile;
* a token is written through DPAPI or not at all (no plaintext fallback).
"""

import json
import os
import sys
import tempfile
import unittest

from src.app.auth.store import (
    AUTH_STATE_FILE,
    DEFAULT_STATE,
    TOKEN_FILE,
    AuthStateStore,
    TokenStore,
)

TOKEN = "gho_fake_access_token_value"


class StoreTestBase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = temp.name

    def path(self, name):
        return os.path.join(self.dir, name)


class AuthStateTest(StoreTestBase):
    def test_missing_file_yields_defaults(self):
        state = AuthStateStore(self.dir).load()
        self.assertEqual(state, dict(DEFAULT_STATE))
        self.assertFalse(state["welcome_seen"])
        self.assertIsNone(state["login"])

    def test_round_trip(self):
        store = AuthStateStore(self.dir)
        login = {
            "provider": "github",
            "login": "octocat",
            "name": "Mona",
            "starred": True,
            "authenticated_at": "2026-10-21T00:00:00Z",
        }
        saved = store.save(
            {
                "welcome_seen": True,
                "star_prompt_pending": True,
                "star_prompt_dismissed": False,
                "login": login,
            }
        )
        self.assertTrue(saved["welcome_seen"])
        self.assertEqual(saved["login"]["login"], "octocat")
        again = AuthStateStore(self.dir).load()
        self.assertEqual(again["login"], login)
        self.assertEqual(again["schema_version"], 1)

    def test_state_file_never_contains_a_token(self):
        store = AuthStateStore(self.dir)
        store.save({"welcome_seen": True, "login": {"provider": "github", "login": "x"}})
        with open(self.path(AUTH_STATE_FILE), "rb") as handle:
            raw = handle.read()
        self.assertNotIn(b"access_token", raw)
        self.assertNotIn(TOKEN.encode(), raw)

    def test_unknown_keys_are_dropped(self):
        with open(self.path(AUTH_STATE_FILE), "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "welcome_seen": True,
                    "access_token": TOKEN,
                    "login": {"provider": "github", "login": "x", "token": TOKEN},
                },
                handle,
            )
        state = AuthStateStore(self.dir).load()
        self.assertNotIn("access_token", state)
        self.assertNotIn("token", state["login"])

    def test_non_boolean_flags_are_ignored(self):
        with open(self.path(AUTH_STATE_FILE), "w", encoding="utf-8") as handle:
            json.dump({"welcome_seen": "yes", "star_prompt_pending": 1}, handle)
        state = AuthStateStore(self.dir).load()
        self.assertFalse(state["welcome_seen"])
        self.assertFalse(state["star_prompt_pending"])

    def test_login_requires_provider_and_name(self):
        store = AuthStateStore(self.dir)
        self.assertIsNone(store.save({"login": {"login": "orphan"}})["login"])
        self.assertIsNone(store.save({"login": {"provider": "github"}})["login"])
        self.assertIsNone(store.save({"login": "not-a-dict"})["login"])

    def test_login_fields_are_truncated(self):
        store = AuthStateStore(self.dir)
        saved = store.save(
            {"login": {"provider": "github", "login": "x" * 500, "name": "y" * 900}}
        )
        self.assertEqual(len(saved["login"]["login"]), 200)
        self.assertEqual(len(saved["login"]["name"]), 200)

    def test_starred_defaults_to_false(self):
        store = AuthStateStore(self.dir)
        saved = store.save({"login": {"provider": "gitee", "login": "u", "starred": "yes"}})
        self.assertFalse(saved["login"]["starred"])

    def test_corrupt_file_starts_from_defaults(self):
        with open(self.path(AUTH_STATE_FILE), "wb") as handle:
            handle.write(b"{ this is not json")
        self.assertEqual(AuthStateStore(self.dir).load(), dict(DEFAULT_STATE))


class TokenStoreTest(StoreTestBase):
    def setUp(self):
        StoreTestBase.setUp(self)
        self.store = TokenStore(self.dir)
        self.payload = {
            "access_token": TOKEN,
            "token_type": "bearer",
            "provider": "github",
            "scope": "read:user",
            "saved_at": "2026-10-21T00:00:00Z",
        }

    def test_missing_token_loads_as_none(self):
        self.assertIsNone(self.store.load())

    def test_available_matches_the_platform(self):
        self.assertEqual(self.store.available(), sys.platform.startswith("win"))

    def test_save_then_load_returns_the_payload(self):
        remembered = self.store.save(self.payload)
        self.assertIsInstance(remembered, bool)
        if remembered:
            self.assertTrue(os.path.exists(self.path(TOKEN_FILE)))
        # A fresh store (no memory cache) must still decrypt it.
        self.assertEqual(TokenStore(self.dir).load(), self.payload)

    def test_token_is_never_written_in_plaintext(self):
        if not self.store.available():
            self.skipTest("DPAPI is unavailable on this platform")
        self.store.save(self.payload)
        with open(self.path(TOKEN_FILE), "rb") as handle:
            raw = handle.read()
        self.assertNotIn(TOKEN.encode(), raw)
        self.assertNotIn(b"access_token", raw)

    def test_save_without_secure_storage_stays_in_memory(self):
        store = TokenStore(self.dir)
        store.available = lambda: False
        self.assertFalse(store.save(self.payload))
        self.assertFalse(os.path.exists(self.path(TOKEN_FILE)))
        self.assertEqual(store.load(), self.payload)

    def test_clear_removes_file_and_memory(self):
        self.store.save(self.payload)
        self.store.clear()
        self.assertFalse(os.path.exists(self.path(TOKEN_FILE)))
        self.assertIsNone(self.store.load())

    def test_clear_without_a_file_does_not_raise(self):
        self.store.clear()

    def test_garbage_file_reads_as_none(self):
        with open(self.path(TOKEN_FILE), "wb") as handle:
            handle.write(b"\x00\x01not-a-dpapi-blob")
        self.assertIsNone(self.store.load())


if __name__ == "__main__":
    unittest.main()
