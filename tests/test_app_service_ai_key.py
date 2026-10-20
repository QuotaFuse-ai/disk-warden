# -*- coding: utf-8 -*-
"""Local API-key storage and the ``ai.test`` RPC (T25)."""

import json
import os
import socket
import unittest
import urllib.error
from unittest import mock

from src.app import PROTOCOL_VERSION
from src.app.bridge import build_methods, handle_request
from src.app.errors import BridgeError
from src.app.service import AppService
from src.core.ai import keystore

from tests.app_fakes import CATALOG_PATH, RULES_PATH, make_service

KEY = "sk-test-do-not-leak-123456"
ENV_KEY = "sk-env-do-not-leak-654321"
AI_PATCH = {
    "base_url": "https://ai.example.test/v1",
    "model": "demo-model",
}


class FakeResponse(object):
    def __init__(self, body):
        self._body = body

    def read(self, size=-1):
        return self._body if size is None or size < 0 else self._body[:size]

    def close(self):
        pass


class FakeOpener(object):
    def __init__(self, result=None):
        self.result = result if result is not None else ok_body()
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        if isinstance(self.result, BaseException):
            raise self.result
        return FakeResponse(self.result)


def ok_body(model="demo-model"):
    return json.dumps({"model": model, "choices": [{"message": {"content": "pong"}}]}).encode(
        "utf-8"
    )


def restart(case, service, environ=None, opener=None):
    restarted = AppService(
        data_root=service.layout.root,
        catalog_path=CATALOG_PATH,
        rules_path=RULES_PATH,
        environ=dict(environ or {}),
        process_provider=lambda: [],
        ai_test_opener=opener,
    )
    case.addCleanup(restarted.close)
    return restarted


@unittest.skipUnless(keystore.is_supported(), "DPAPI is only available on Windows")
class StoredKeyTest(unittest.TestCase):
    def setUp(self):
        self.opener = FakeOpener()
        self.service = make_service(self, ai_test_opener=self.opener)

    def save(self, key=KEY, **extra):
        patch = dict(AI_PATCH, api_key=key)
        patch.update(extra)
        return self.service.settings_set({"ai": patch})

    def settings_file_text(self):
        with open(self.service.settings_store.path, "r", encoding="utf-8") as handle:
            return handle.read()

    def test_default_source_is_none(self):
        ai = self.service.settings_get()["ai"]
        self.assertFalse(ai["api_key_set"])
        self.assertEqual(ai["api_key_source"], "none")
        self.assertEqual(ai["api_key_error"], "")

    def test_saved_key_is_reported_as_stored(self):
        ai = self.save()["ai"]
        self.assertTrue(ai["api_key_set"])
        self.assertEqual(ai["api_key_source"], "stored")
        self.assertEqual(ai["api_key_error"], "")
        self.assertTrue(keystore.stored(self.service.layout.ai_key_path))

    def test_no_returned_value_contains_the_key(self):
        payload = self.save()
        self.assertNotIn(KEY, json.dumps(payload))
        self.assertNotIn(KEY, json.dumps(self.service.settings_get()))
        self.assertNotIn(KEY, repr(self.service._ai_config))
        ai = payload["ai"]
        self.assertEqual(ai["base_url"], AI_PATCH["base_url"])
        self.assertEqual(ai["model"], AI_PATCH["model"])

    def test_no_file_contains_the_key(self):
        self.save()
        self.assertNotIn(KEY, self.settings_file_text())
        with open(self.service.layout.ai_key_path, "rb") as handle:
            self.assertNotIn(KEY.encode("utf-8"), handle.read())

    def test_saved_key_survives_a_restart(self):
        self.save()
        restarted = restart(self, self.service)
        ai = restarted.settings_get()["ai"]
        self.assertTrue(ai["api_key_set"])
        self.assertEqual(ai["api_key_source"], "stored")

    def test_saved_key_is_used_for_requests(self):
        self.save()
        self.assertTrue(self.service.ai_test()["ok"])
        request = self.opener.requests[-1]
        self.assertEqual(request.get_header("Authorization"), "Bearer " + KEY)

    def test_environment_key_takes_precedence(self):
        service = make_service(
            self,
            environ={"DISK_WARDEN_AI_API_KEY": ENV_KEY},
            ai_test_opener=self.opener,
        )
        self.service = service
        service.settings_set({"ai": dict(AI_PATCH, api_key=KEY)})
        ai = service.settings_get()["ai"]
        self.assertEqual(ai["api_key_source"], "env")
        self.assertTrue(ai["api_key_set"])
        self.assertTrue(service.ai_test()["ok"])
        self.assertEqual(
            self.opener.requests[-1].get_header("Authorization"),
            "Bearer " + ENV_KEY,
        )

    def test_empty_string_clears_the_saved_key(self):
        self.save()
        ai = self.service.settings_set({"ai": {"api_key": ""}})["ai"]
        self.assertFalse(ai["api_key_set"])
        self.assertEqual(ai["api_key_source"], "none")
        self.assertFalse(keystore.stored(self.service.layout.ai_key_path))

    def test_camel_case_alias_is_accepted(self):
        ai = self.service.settings_set({"ai": dict(AI_PATCH, apiKey=KEY)})["ai"]
        self.assertTrue(ai["api_key_set"])
        self.assertEqual(ai["api_key_source"], "stored")

    def test_whitespace_only_key_clears_instead_of_storing(self):
        self.save()
        ai = self.service.settings_set({"ai": {"api_key": "   "}})["ai"]
        self.assertFalse(ai["api_key_set"])
        self.assertFalse(keystore.stored(self.service.layout.ai_key_path))

    def test_overlong_key_is_rejected(self):
        with self.assertRaises(BridgeError) as ctx:
            self.save("x" * (keystore.MAX_SECRET_BYTES + 1))
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")
        self.assertFalse(keystore.stored(self.service.layout.ai_key_path))

    def test_key_is_saved_when_ai_is_enabled(self):
        self.service.ai_consent()
        ai = self.save(enabled=True)["ai"]
        self.assertTrue(ai["available"])
        self.assertEqual(ai["disabled_reason"], "")

    def test_clearing_the_key_makes_ai_unavailable(self):
        self.service.ai_consent()
        self.save(enabled=True)
        ai = self.service.settings_set({"ai": {"api_key": ""}})["ai"]
        self.assertFalse(ai["available"])
        self.assertEqual(ai["disabled_reason"], "missing_api_key")

    def test_consent_gate_does_not_persist_the_key(self):
        with self.assertRaises(BridgeError) as ctx:
            self.save(enabled=True)
        self.assertEqual(ctx.exception.code, "AI_CONSENT_REQUIRED")
        self.assertFalse(keystore.stored(self.service.layout.ai_key_path))

    def test_tampered_key_file_is_reported_without_crashing(self):
        self.save()
        path = self.service.layout.ai_key_path
        with open(path, "rb") as handle:
            raw = handle.read()
        with open(path, "wb") as handle:
            handle.write(b"not a dpapi blob")
        try:
            restarted = restart(self, self.service)
            ai = restarted.settings_get()["ai"]
            self.assertFalse(ai["api_key_set"])
            self.assertEqual(ai["api_key_source"], "stored")
            self.assertEqual(ai["api_key_error"], keystore.CODE_CORRUPT)
            self.assertEqual(restarted.hello()["settings"]["ai"]["api_key_source"], "stored")
        finally:
            with open(path, "wb") as handle:
                handle.write(raw)


class AITestRPCTest(unittest.TestCase):
    def setUp(self):
        self.opener = FakeOpener()
        self.service = make_service(self, ai_test_opener=self.opener)

    def configure(self, **overrides):
        patch = dict(AI_PATCH, api_key=KEY)
        patch.update(overrides)
        self.service.settings_set({"ai": patch})

    def test_success_reports_the_model_and_latency(self):
        self.configure()
        result = self.service.ai_test()
        self.assertTrue(result["ok"])
        self.assertEqual(result["model"], "demo-model")
        self.assertEqual(result["error_code"], "")
        self.assertIsInstance(result["latency_ms"], int)
        self.assertGreaterEqual(result["latency_ms"], 0)
        self.assertNotIn(KEY, json.dumps(result))

    def test_request_is_minimal(self):
        self.configure()
        self.service.ai_test()
        request = self.opener.requests[-1]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["model"], "demo-model")
        self.assertEqual(body["max_tokens"], 1)
        self.assertFalse(body["stream"])
        self.assertTrue(request.full_url.endswith("/chat/completions"))

    def test_missing_key_returns_a_stable_code(self):
        self.service.settings_set({"ai": dict(AI_PATCH)})
        result = self.service.ai_test()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "missing_api_key")
        self.assertEqual(self.opener.requests, [])

    def test_missing_endpoint_and_model_are_reported(self):
        result = self.service.ai_test({"api_key": KEY})
        self.assertEqual(result["error_code"], "missing_base_url")
        result = self.service.ai_test({"api_key": KEY, "base_url": AI_PATCH["base_url"]})
        self.assertEqual(result["error_code"], "missing_model")

    def test_timeout_is_reported_inline(self):
        self.configure()
        self.opener.result = socket.timeout("timed out")
        with self.assertLogs("disk_warden.app", level="DEBUG") as captured:
            result = self.service.ai_test()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "timeout")
        self.assertNotIn(KEY, "\n".join(captured.output))
        self.assertNotIn(KEY, json.dumps(result))

    def test_http_error_is_reported_inline(self):
        self.configure()
        self.opener.result = urllib.error.HTTPError(
            "https://ai.example.test/v1/chat/completions", 401, "Unauthorized", {}, None
        )
        result = self.service.ai_test()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "http")
        self.assertNotIn(KEY, result["message"])

    def test_unsaved_form_values_are_used_without_persisting(self):
        result = self.service.ai_test(dict(AI_PATCH, api_key=KEY))
        self.assertTrue(result["ok"])
        self.assertFalse(keystore.stored(self.service.layout.ai_key_path))
        with open(self.service.settings_store.path, "r", encoding="utf-8") as handle:
            self.assertNotIn(KEY, handle.read())

    def test_api_key_override_must_be_a_string(self):
        with self.assertRaises(BridgeError) as ctx:
            self.service.ai_test({"api_key": 42})
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")


class BridgeWiringTest(unittest.TestCase):
    def test_ai_test_receives_params(self):
        service = mock.Mock()
        service.ai_test.return_value = {"ok": True}
        response = handle_request(
            service,
            {
                "v": PROTOCOL_VERSION,
                "id": "1",
                "method": "ai.test",
                "params": {"model": "m"},
            },
            build_methods(service),
        )
        self.assertTrue(response["ok"])
        service.ai_test.assert_called_once_with({"model": "m"})


if __name__ == "__main__":
    unittest.main()
