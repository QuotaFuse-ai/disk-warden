# -*- coding: utf-8 -*-
"""Resolver tests for the AI layer (T22).

The fake transport makes every scenario offline: only yellow items trigger a
request, AI verdicts always need confirmation, and every failure degrades back
to yellow instead of interrupting the scan.
"""

import json
import logging
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from src.core.ai.cache import AICache
from src.core.ai.config import AIConfig
from src.core.ai.payload import serialize_payload
from src.core.ai.provider import CODE_TIMEOUT, ProviderError
from src.core.ai.resolver import (
    AIResolver,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_request,
    estimate_tokens,
    parse_suggestion,
)
from src.core.rules import load_engine
from src.core.rules.models import (
    SAFETY_GREEN,
    SAFETY_RED,
    SAFETY_YELLOW,
    SOURCE_AI,
    Classification,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULES_PATH = os.path.join(ROOT, "data", "rules.json")
CATALOG_PATH = os.path.join(ROOT, "data", "ide-catalog.json")

ENV = {
    "USERNAME": "alice",
    "USERPROFILE": r"C:\Users\alice",
    "APPDATA": r"C:\Users\alice\AppData\Roaming",
    "LOCALAPPDATA": r"C:\Users\alice\AppData\Local",
}
APPDATA = ENV["APPDATA"]

LOGGER = logging.getLogger("test_ai_resolver")
LOGGER.addHandler(logging.NullHandler())
LOGGER.propagate = False


def make_config(**overrides):
    values = {
        "enabled": True,
        "base_url": "https://api.example.test/v1",
        "model": "test-model",
        "api_key": "sk-test-secret",
        "max_calls": 20,
        "max_total_tokens": 60000,
        "max_output_tokens": 200,
        "timeout_seconds": 5.0,
        "ttl_seconds": 3600,
        "max_path_segments": 3,
        "cache_path": "",
    }
    values.update(overrides)
    return AIConfig(**values)


def envelope(safety="green", reason="regenerable cache", usage=42, text=None):
    content = text if text is not None else json.dumps(
        {"safety": safety, "reason": reason}
    )
    data = {"choices": [{"message": {"content": content}}]}
    if usage is not None:
        data["usage"] = {"total_tokens": usage}
    return data


class FakeProvider(object):
    def __init__(self, response=None, error=None):
        self.response = envelope() if response is None else response
        self.error = error
        self.requests = []

    def classify(self, payload):
        self.requests.append(payload)
        if self.error is not None:
            raise self.error
        return self.response


def item(leaf="mystery-blob", tool_id="cursor"):
    return SimpleNamespace(
        tool_id=tool_id,
        source_path=APPDATA + "\\Cursor\\" + leaf,
        rel_path="Cursor/" + leaf,
        kind="dir",
        item_id=None,
        bytes=1024,
        file_count=8,
    )


def current(safety=SAFETY_YELLOW):
    return Classification(
        item_id="x",
        safety=safety,
        source="unknown",
        rule_id="unknown",
        reason="no rule matched",
        reason_key="rule.reason.unknown",
        requires_confirm=safety == SAFETY_YELLOW,
    )


def make_resolver(provider, cache=None, **overrides):
    return AIResolver(
        provider,
        make_config(**overrides),
        cache=cache,
        rules_version="rules-v1",
        environ=ENV,
        logger=LOGGER,
    )


class YellowOnlyTests(unittest.TestCase):
    def test_green_and_red_never_call_the_provider(self):
        provider = FakeProvider()
        resolver = make_resolver(provider)
        self.assertIsNone(resolver.classify(item(), current(SAFETY_GREEN)))
        self.assertIsNone(resolver.classify(item(), current(SAFETY_RED)))
        self.assertEqual(provider.requests, [])
        self.assertEqual(resolver.stats()["calls"], 0)

    def test_disabled_resolver_never_calls_the_provider(self):
        provider = FakeProvider()
        resolver = make_resolver(provider, enabled=False)
        self.assertIsNone(resolver.classify(item(), current()))
        self.assertEqual(provider.requests, [])

    def test_missing_key_never_calls_the_provider(self):
        provider = FakeProvider()
        resolver = make_resolver(provider, api_key="")
        self.assertIsNone(resolver.classify(item(), current()))
        self.assertEqual(provider.requests, [])
        self.assertFalse(resolver.stats()["available"])


class PayloadBoundaryTests(unittest.TestCase):
    def test_sent_payload_is_sanitized(self):
        provider = FakeProvider()
        resolver = make_resolver(provider)
        resolver.classify(item(), current())

        request = provider.requests[0]
        self.assertEqual(request["model"], "test-model")
        self.assertEqual(request["temperature"], 0)
        self.assertEqual(request["messages"][0]["content"], SYSTEM_PROMPT)
        user_content = request["messages"][1]["content"]
        self.assertIn("Cursor/mystery-blob", user_content)
        lowered = user_content.casefold()
        self.assertNotIn("alice", lowered)
        self.assertNotIn("c:", lowered)
        self.assertNotIn("users", lowered)

    def test_leaking_payload_is_refused_before_the_request(self):
        provider = FakeProvider()
        resolver = make_resolver(provider)
        leaky = {
            "schema_version": 1,
            "path": "C:/Users/alice/secret",
            "path_segments": ["C:", "Users", "alice", "secret"],
            "depth": 4,
            "file_count": 1,
            "total_bytes": 10,
            "extensions": {},
        }
        with mock.patch(
            "src.core.ai.resolver.build_payload", return_value=leaky
        ):
            self.assertIsNone(resolver.classify(item(), current()))

        self.assertEqual(provider.requests, [])
        self.assertEqual(resolver.stats()["refused_payloads"], 1)


class SuggestionTests(unittest.TestCase):
    def test_green_suggestion_is_returned(self):
        resolver = make_resolver(FakeProvider())
        suggestion = resolver.classify(item(), current())
        self.assertEqual(suggestion.safety, SAFETY_GREEN)
        self.assertEqual(suggestion.reason, "regenerable cache")
        self.assertEqual(resolver.stats()["suggestions"], 1)

    def test_red_suggestion_is_returned(self):
        resolver = make_resolver(FakeProvider(envelope("red", "holds tokens")))
        suggestion = resolver.classify(item(), current())
        self.assertEqual(suggestion.safety, SAFETY_RED)

    def test_usage_tokens_are_recorded(self):
        resolver = make_resolver(FakeProvider(envelope(usage=123)))
        resolver.classify(item(), current())
        self.assertEqual(resolver.stats()["estimated_tokens"], 123)

    def test_estimated_tokens_are_used_without_usage(self):
        resolver = make_resolver(FakeProvider(envelope(usage=None)))
        resolver.classify(item(), current())
        self.assertGreater(resolver.stats()["estimated_tokens"], 0)


class DegradationTests(unittest.TestCase):
    def _assert_yellow(self, resolver, provider):
        self.assertIsNone(resolver.classify(item(), current()))
        self.assertEqual(len(provider.requests), 1)
        stats = resolver.stats()
        self.assertEqual(stats["calls"], 1)
        self.assertEqual(stats["failures"], 1)
        self.assertEqual(stats["suggestions"], 0)

    def test_timeout(self):
        provider = FakeProvider(error=ProviderError(CODE_TIMEOUT, "timed out"))
        self._assert_yellow(make_resolver(provider), provider)

    def test_rate_limit(self):
        provider = FakeProvider(error=ProviderError("rate_limited", "429", 429))
        self._assert_yellow(make_resolver(provider), provider)

    def test_unexpected_provider_exception(self):
        provider = FakeProvider(error=RuntimeError("boom"))
        self._assert_yellow(make_resolver(provider), provider)

    def test_invalid_json_content(self):
        provider = FakeProvider(envelope(text="I think it is a cache"))
        self._assert_yellow(make_resolver(provider), provider)

    def test_unknown_safety_value(self):
        provider = FakeProvider(envelope(text='{"safety":"purple","reason":"?"}'))
        self._assert_yellow(make_resolver(provider), provider)

    def test_yellow_answer_keeps_item_yellow(self):
        provider = FakeProvider(envelope(safety="yellow"))
        self._assert_yellow(make_resolver(provider), provider)


class BudgetTests(unittest.TestCase):
    def test_call_cap_is_enforced(self):
        provider = FakeProvider()
        resolver = make_resolver(provider, max_calls=2)
        results = [
            resolver.classify(item("blob-%d" % index), current())
            for index in range(3)
        ]
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(results[2], None)
        stats = resolver.stats()
        self.assertEqual(stats["calls"], 2)
        self.assertEqual(stats["budget_skips"], 1)

    def test_zero_call_cap_disables_requests(self):
        provider = FakeProvider()
        resolver = make_resolver(provider, max_calls=0)
        self.assertIsNone(resolver.classify(item(), current()))
        self.assertEqual(provider.requests, [])
        self.assertEqual(resolver.stats()["budget_skips"], 1)

    def test_token_cap_is_enforced(self):
        provider = FakeProvider()
        resolver = make_resolver(provider, max_total_tokens=1)
        self.assertIsNone(resolver.classify(item(), current()))
        self.assertEqual(provider.requests, [])
        self.assertEqual(resolver.stats()["budget_skips"], 1)

    def test_zero_token_cap_disables_the_limit(self):
        provider = FakeProvider()
        resolver = make_resolver(provider, max_total_tokens=0)
        self.assertIsNotNone(resolver.classify(item(), current()))
        self.assertEqual(len(provider.requests), 1)


class CacheTests(unittest.TestCase):
    def test_cache_hit_issues_no_request(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "ai-cache.json")
            first_provider = FakeProvider()
            first = make_resolver(first_provider, cache=AICache(path))
            original = first.classify(item(), current())
            self.assertEqual(len(first_provider.requests), 1)
            self.assertTrue(os.path.exists(path))

            second_provider = FakeProvider()
            second = make_resolver(second_provider, cache=AICache(path))
            cached = second.classify(item(), current())

            self.assertEqual(second_provider.requests, [])
            self.assertEqual(cached.safety, original.safety)
            self.assertEqual(cached.reason, original.reason)
            stats = second.stats()
            self.assertEqual(stats["cache_hits"], 1)
            self.assertEqual(stats["calls"], 0)

    def test_rules_version_invalidates_the_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "ai-cache.json")
            first_provider = FakeProvider()
            first = AIResolver(
                first_provider,
                make_config(),
                cache=AICache(path),
                rules_version="rules-v1",
                environ=ENV,
                logger=LOGGER,
            )
            first.classify(item(), current())

            second_provider = FakeProvider()
            second = AIResolver(
                second_provider,
                make_config(),
                cache=AICache(path),
                rules_version="rules-v2",
                environ=ENV,
                logger=LOGGER,
            )
            second.classify(item(), current())
            self.assertEqual(len(second_provider.requests), 1)
            self.assertEqual(second.stats()["cache_hits"], 0)

    def test_prompt_version_is_part_of_the_key(self):
        self.assertEqual(PROMPT_VERSION, "1")


class StatsTests(unittest.TestCase):
    def test_stats_never_expose_the_key(self):
        resolver = make_resolver(FakeProvider())
        resolver.classify(item(), current())
        stats = resolver.stats()
        self.assertNotIn("sk-test-secret", repr(stats))
        self.assertNotIn("api_key", stats)
        self.assertTrue(stats["enabled"])
        self.assertTrue(stats["available"])
        self.assertEqual(stats["max_calls"], 20)
        self.assertEqual(stats["max_total_tokens"], 60000)


class EngineIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.provider = FakeProvider()
        self.resolver = make_resolver(self.provider)
        self.engine = load_engine(
            RULES_PATH,
            CATALOG_PATH,
            environ=ENV,
            ai_resolver=self.resolver,
        )

    def test_green_item_does_not_trigger_ai(self):
        result = self.engine.classify(item("Cache"))
        self.assertEqual(result.safety, SAFETY_GREEN)
        self.assertNotEqual(result.source, SOURCE_AI)
        self.assertEqual(self.provider.requests, [])

    def test_red_item_is_terminal(self):
        result = self.engine.classify(item("Login Data"))
        self.assertEqual(result.safety, SAFETY_RED)
        self.assertEqual(self.provider.requests, [])

    def test_ai_red_is_not_overridden(self):
        self.provider.response = envelope("green", "looks like a cache")
        result = self.engine.classify(item("Login Data"))
        self.assertEqual(result.safety, SAFETY_RED)
        self.assertEqual(self.provider.requests, [])

    def test_yellow_item_gets_ai_suggestion_with_confirmation(self):
        result = self.engine.classify(item("mystery-blob"))
        self.assertEqual(len(self.provider.requests), 1)
        self.assertEqual(result.safety, SAFETY_GREEN)
        self.assertEqual(result.source, SOURCE_AI)
        self.assertEqual(result.rule_id, "ai")
        self.assertTrue(result.requires_confirm)
        self.assertFalse(result.can_migrate)

    def test_ai_can_only_suggest_green_or_red(self):
        self.provider.response = envelope("red", "contains tokens")
        result = self.engine.classify(item("mystery-blob"))
        self.assertEqual(result.safety, SAFETY_RED)
        self.assertEqual(result.source, SOURCE_AI)
        self.assertTrue(result.requires_confirm)
        self.assertFalse(result.can_migrate)

    def test_offline_keeps_yellow(self):
        self.provider.error = ProviderError(CODE_TIMEOUT, "timed out")
        result = self.engine.classify(item("mystery-blob"))
        self.assertEqual(result.safety, SAFETY_YELLOW)
        self.assertTrue(result.requires_confirm)
        self.assertFalse(result.can_migrate)
        self.assertEqual(self.resolver.stats()["failures"], 1)


class PromptTests(unittest.TestCase):
    def test_build_request_shape(self):
        request = build_request({"path": "Cursor/Cache"}, "test-model", 128)
        self.assertEqual(request["model"], "test-model")
        self.assertEqual(request["temperature"], 0)
        self.assertEqual(request["max_tokens"], 128)
        self.assertEqual(request["messages"][0]["role"], "system")
        self.assertEqual(request["messages"][1]["role"], "user")

    def test_user_message_is_json_metadata(self):
        payload = {"path": "Cursor/Cache", "file_count": 3}
        request = build_request(payload, "test-model")
        content = request["messages"][1]["content"]
        self.assertIn(serialize_payload(payload), content)

    def test_estimate_tokens_is_positive(self):
        request = build_request({"path": "a"}, "m", 50)
        self.assertGreater(estimate_tokens(request), 50)


class ParseSuggestionTests(unittest.TestCase):
    def test_plain_json(self):
        suggestion = parse_suggestion('{"safety":"green","reason":"cache"}')
        self.assertEqual(suggestion.safety, SAFETY_GREEN)

    def test_code_fence(self):
        suggestion = parse_suggestion(
            '```json\n{"safety":"red","reason":"credentials"}\n```'
        )
        self.assertEqual(suggestion.safety, SAFETY_RED)

    def test_prose_around_json(self):
        suggestion = parse_suggestion(
            'Sure: {"safety":"green","reason":"logs"} hope that helps'
        )
        self.assertEqual(suggestion.safety, SAFETY_GREEN)

    def test_yellow_means_no_suggestion(self):
        self.assertIsNone(
            parse_suggestion('{"safety":"yellow","reason":"not sure"}')
        )

    def test_invalid_json(self):
        self.assertIsNone(parse_suggestion("definitely a cache"))

    def test_missing_reason(self):
        self.assertIsNone(parse_suggestion('{"safety":"green"}'))

    def test_unknown_safety(self):
        self.assertIsNone(
            parse_suggestion('{"safety":"orange","reason":"maybe"}')
        )

    def test_reason_is_truncated(self):
        reason = "x" * 500
        suggestion = parse_suggestion(
            json.dumps({"safety": "green", "reason": reason})
        )
        self.assertEqual(len(suggestion.reason), 200)

    def test_control_characters_are_removed(self):
        suggestion = parse_suggestion(
            '{"safety":"green","reason":"cache\\u0000data"}'
        )
        self.assertNotIn("\x00", suggestion.reason)


if __name__ == "__main__":
    unittest.main()
