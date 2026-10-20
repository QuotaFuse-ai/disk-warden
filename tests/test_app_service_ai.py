# -*- coding: utf-8 -*-
"""AI consent gate and settings persistence (T12 batch 3, module 3)."""

import json
import os
import unittest

from src.app.errors import BridgeError
from src.app.service import AppService

from tests.app_fakes import CATALOG_PATH, RULES_PATH, make_service

API_KEY = "sk-test-do-not-leak-123456"
AI_ENV = {
    "DISK_WARDEN_AI_API_KEY": API_KEY,
}
AI_PATCH = {
    "base_url": "https://ai.example.test/v1",
    "model": "demo-model",
}


class AISettingsTestBase(unittest.TestCase):
    def setUp(self):
        self.service = make_service(self)

    def enable(self, patch=None):
        settings = dict(AI_PATCH)
        settings.update(patch or {})
        settings["enabled"] = True
        return self.service.settings_set({"ai": settings})["ai"]

    def assert_invalid(self, patch, fragment=""):
        with self.assertRaises(BridgeError) as ctx:
            self.service.settings_set(patch)
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")
        if fragment:
            self.assertIn(fragment, ctx.exception.detail.get("reason", ""))


class AIDefaultsTest(AISettingsTestBase):
    def test_ai_is_off_by_default(self):
        ai = self.service.settings_get()["ai"]
        self.assertFalse(ai["enabled"])
        self.assertFalse(ai["available"])
        self.assertFalse(ai["consent"])
        self.assertFalse(ai["api_key_set"])
        self.assertEqual(ai["disabled_reason"], "disabled")
        self.assertEqual(ai["warning_key"], "ai.third_party_warning")
        self.assertIn("第三方", ai["warning"])
        self.assertEqual(ai["base_url"], "")
        self.assertEqual(ai["model"], "")

    def test_status_matches_settings_get(self):
        self.assertEqual(self.service.ai_status(), self.service.settings_get()["ai"])

    def test_consent_alone_does_not_enable_ai(self):
        ai = self.service.ai_consent()
        self.assertTrue(ai["consent"])
        self.assertFalse(ai["enabled"])
        self.assertEqual(ai["disabled_reason"], "disabled")


class AIConsentGateTest(AISettingsTestBase):
    def test_enabling_without_consent_is_blocked(self):
        with self.assertRaises(BridgeError) as ctx:
            self.enable()
        error = ctx.exception
        self.assertEqual(error.code, "AI_CONSENT_REQUIRED")
        self.assertEqual(error.message_key, "error.AI_CONSENT_REQUIRED")
        self.assertFalse(error.retryable)
        self.assertEqual(
            error.detail.get("warning_key"), "ai.third_party_warning"
        )

        ai = self.service.settings_get()["ai"]
        self.assertFalse(ai["enabled"])
        self.assertFalse(ai["consent"])

    def test_explicit_consent_false_is_also_blocked(self):
        self.service.ai_consent()
        with self.assertRaises(BridgeError) as ctx:
            self.service.settings_set(
                {"ai": {"enabled": True, "consent": False}}
            )
        self.assertEqual(ctx.exception.code, "AI_CONSENT_REQUIRED")
        self.assertFalse(self.service.settings_get()["ai"]["enabled"])

    def test_blocked_enable_is_not_persisted(self):
        with self.assertRaises(BridgeError):
            self.enable()
        with open(self.service.settings_store.path, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertFalse(saved.get("ai_enabled", False))
        self.assertFalse(saved.get("ai_consent", False))
        self.assertNotIn("ai_base_url", saved)

    def test_consent_then_enable_succeeds(self):
        service = make_service(self, environ=AI_ENV)
        service.ai_consent()
        ai = service.settings_set(
            {"ai": dict(AI_PATCH, enabled=True)}
        )["ai"]
        self.assertTrue(ai["consent"])
        self.assertTrue(ai["enabled"])
        self.assertTrue(ai["available"])
        self.assertEqual(ai["base_url"], AI_PATCH["base_url"])
        self.assertEqual(ai["model"], AI_PATCH["model"])


class AIAvailabilityTest(AISettingsTestBase):
    def setUp(self):
        self.service = make_service(self, environ=AI_ENV)
        self.service.ai_consent()

    def test_full_configuration_is_available(self):
        ai = self.enable()
        self.assertTrue(ai["available"])
        self.assertEqual(ai["disabled_reason"], "")
        self.assertTrue(ai["api_key_set"])
        self.assertEqual(ai["base_url"], AI_PATCH["base_url"])
        self.assertEqual(ai["model"], AI_PATCH["model"])

    def test_api_key_is_detected_while_ai_is_disabled(self):
        ai = self.service.settings_get()["ai"]
        self.assertFalse(ai["enabled"])
        self.assertTrue(ai["api_key_set"])
        self.assertEqual(ai["disabled_reason"], "disabled")

    def test_missing_key_keeps_ai_unavailable(self):
        service = make_service(self)
        service.ai_consent()
        ai = service.settings_set({"ai": dict(AI_PATCH, enabled=True)})["ai"]
        self.assertTrue(ai["enabled"])
        self.assertFalse(ai["available"])
        self.assertFalse(ai["api_key_set"])
        self.assertEqual(ai["disabled_reason"], "missing_api_key")

    def test_missing_base_url_then_missing_model(self):
        ai = self.service.settings_set(
            {"ai": {"enabled": True, "model": "demo-model"}}
        )["ai"]
        self.assertEqual(ai["disabled_reason"], "missing_base_url")

        fresh = make_service(self, environ=AI_ENV)
        fresh.ai_consent()
        ai = fresh.settings_set(
            {"ai": {"enabled": True, "base_url": "https://ai.example.test/v1"}}
        )["ai"]
        self.assertEqual(ai["disabled_reason"], "missing_model")

    def test_disabling_ai_drops_availability(self):
        self.enable()
        ai = self.service.settings_set({"ai": {"enabled": False}})["ai"]
        self.assertFalse(ai["enabled"])
        self.assertFalse(ai["available"])
        self.assertEqual(ai["disabled_reason"], "disabled")
        self.assertTrue(ai["consent"])

    def test_base_url_and_model_are_stripped(self):
        ai = self.service.settings_set(
            {
                "ai": {
                    "enabled": True,
                    "base_url": "  https://ai.example.test/v1  ",
                    "model": "  demo-model  ",
                }
            }
        )["ai"]
        self.assertEqual(ai["base_url"], "https://ai.example.test/v1")
        self.assertEqual(ai["model"], "demo-model")

    def test_secret_never_appears_in_public_output(self):
        self.enable()
        rendered = json.dumps(self.service.settings_get(), sort_keys=True)
        self.assertNotIn(API_KEY, rendered)
        self.assertNotIn(API_KEY, repr(self.service._ai_config))


class AISettingsValidationTest(AISettingsTestBase):
    def test_api_key_cannot_be_supplied_through_settings(self):
        for key in ("api_key", "apiKey"):
            with self.assertRaises(BridgeError) as ctx:
                self.service.settings_set({"ai": {key: API_KEY}})
            self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")
            self.assertIn(
                "DISK_WARDEN_AI_API_KEY",
                ctx.exception.detail.get("reason", ""),
            )

    def test_ai_must_be_an_object(self):
        self.assert_invalid({"ai": "on"}, "ai settings must be an object")

    def test_ai_booleans_are_strict(self):
        for key in ("enabled", "consent"):
            self.assert_invalid({"ai": {key: "true"}}, "must be a boolean")
            self.assert_invalid({"ai": {key: 1}}, "must be a boolean")

    def test_ai_strings_are_strict(self):
        for key in ("base_url", "model"):
            self.assert_invalid({"ai": {key: 42}}, "must be a string")
            self.assert_invalid({"ai": {key: None}}, "must be a string")


class SettingsValidationTest(AISettingsTestBase):
    def test_patch_must_be_an_object(self):
        for patch in (None, [], "x", 7):
            with self.assertRaises(BridgeError) as ctx:
                self.service.settings_set(patch)
            self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")

    def test_boolean_and_policy_settings_are_validated(self):
        self.assert_invalid({"launch_at_startup": "yes"}, "boolean")
        self.assert_invalid({"auto_maintenance": 1}, "boolean")
        self.assert_invalid({"uninstall_policy": "delete"}, "keep or rollback")
        self.assert_invalid({"language": ""}, "non-empty string")
        self.assert_invalid({"language": 5}, "non-empty string")

    def test_valid_settings_are_persisted(self):
        settings = self.service.settings_set(
            {
                "launch_at_startup": True,
                "auto_maintenance": True,
                "uninstall_policy": "rollback",
                "language": "  zh-CN  ",
            }
        )
        self.assertTrue(settings["launch_at_startup"])
        self.assertTrue(settings["auto_maintenance"])
        self.assertEqual(settings["uninstall_policy"], "rollback")
        self.assertEqual(settings["language"], "zh-CN")


class AIPersistenceTest(AISettingsTestBase):
    def test_ai_choice_survives_a_restart(self):
        service = make_service(self, environ=AI_ENV)
        service.ai_consent()
        service.settings_set({"ai": dict(AI_PATCH, enabled=True)})

        restarted = AppService(
            data_root=service.layout.root,
            catalog_path=CATALOG_PATH,
            rules_path=RULES_PATH,
            environ=dict(AI_ENV),
            process_provider=lambda: [],
        )
        self.addCleanup(restarted.close)
        ai = restarted.settings_get()["ai"]
        self.assertTrue(ai["enabled"])
        self.assertTrue(ai["consent"])
        self.assertTrue(ai["available"])
        self.assertEqual(ai["base_url"], AI_PATCH["base_url"])
        self.assertEqual(ai["model"], AI_PATCH["model"])
        self.assertNotIn(API_KEY, json.dumps(restarted.settings_get()))

    def test_consent_survives_a_restart(self):
        self.service.ai_consent()
        restarted = AppService(
            data_root=self.service.layout.root,
            catalog_path=CATALOG_PATH,
            rules_path=RULES_PATH,
            environ={},
            process_provider=lambda: [],
        )
        self.addCleanup(restarted.close)
        ai = restarted.settings_get()["ai"]
        self.assertTrue(ai["consent"])
        self.assertFalse(ai["enabled"])
        self.assertEqual(ai["disabled_reason"], "disabled")


if __name__ == "__main__":
    unittest.main()
