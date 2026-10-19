# -*- coding: utf-8 -*-
"""CLI wiring for the opt-in AI analysis (T22).

These tests drive ``python -m src.core.scan`` entry points without touching the
network: the discovery/measurement layers are replaced with fakes and the AI
provider class is patched with a local stub.

Python 3.8 compatible (decision 0005).
"""

import argparse
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest import mock

from src.core.rules import RulesError
from src.core.rules.models import (
    SAFETY_GREEN,
    SAFETY_RED,
    SAFETY_YELLOW,
    SOURCE_AI,
    SOURCE_UNKNOWN,
)
from src.core.scan import __main__ as cli

FULL_ENV = {
    "USERNAME": "alice",
    "USERPROFILE": r"C:\Users\alice",
    "APPDATA": r"C:\Users\alice\AppData\Roaming",
    "LOCALAPPDATA": r"C:\Users\alice\AppData\Local",
    "DISK_WARDEN_AI_BASE_URL": "https://ai.example.invalid/v1",
    "DISK_WARDEN_AI_MODEL": "test-model",
    "DISK_WARDEN_AI_API_KEY": "sk-test-secret",
}

AI_RESPONSE = {
    "choices": [
        {
            "message": {
                "content": json.dumps(
                    {"safety": "green", "reason": "regenerable cache data"}
                )
            }
        }
    ],
    "usage": {"total_tokens": 123},
}


class FakeItem(object):
    def __init__(self, item_id, tool_id, source_path, rel_path, kind="dir"):
        self.item_id = item_id
        self.tool_id = tool_id
        self.source_path = source_path
        self.rel_path = rel_path
        self.kind = kind
        self.bytes = 4096
        self.file_count = 12
        self.is_reparse = False
        self.access_errors = 0
        self.mtime_max = None


class FakeSizeReport(object):
    def __init__(self, items):
        self.items = items
        self.warnings = []

    def summary(self):
        return {
            "items": len(self.items),
            "files": sum(item.file_count for item in self.items),
            "bytes": sum(item.bytes for item in self.items),
            "access_errors": 0,
        }

    def to_dict(self):
        return {
            "summary": self.summary(),
            "items": [item.__dict__ for item in self.items],
        }


class FakeDiscovery(object):
    def __init__(self):
        self.tools = []
        self.warnings = []

    def summary(self):
        return {
            "tools": {
                "total": 0,
                "present": 0,
                "absent": 0,
                "no_permission": 0,
            },
            "directories": {
                "total": 0,
                "present": 0,
                "absent": 0,
                "no_permission": 0,
            },
            "warnings": {"total": 0},
        }

    def to_dict(self):
        return {"summary": self.summary(), "tools": [], "warnings": []}


class FakeProvider(object):
    instances = []

    def __init__(self, base_url, model, api_key, timeout_seconds=None):
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.requests = []
        FakeProvider.instances.append(self)

    def classify(self, request):
        self.requests.append(request)
        return json.loads(json.dumps(AI_RESPONSE))


class CliTestCase(unittest.TestCase):
    def setUp(self):
        FakeProvider.instances = []
        self.addCleanup(self._clear_ai_logger)

    @staticmethod
    def _clear_ai_logger():
        import logging

        logger = logging.getLogger("disk_warden.ai")
        for handler in list(logger.handlers):
            logger.removeHandler(handler)

    def _run_main(self, argv, size_items, provider_cls=FakeProvider,
                  engine_error=None, ai_cache_dir=None):
        report = FakeSizeReport(size_items)
        patches = [
            mock.patch.object(cli, "discover", return_value=FakeDiscovery()),
            mock.patch.object(cli, "measure", return_value=report),
            mock.patch.object(cli, "targets_from_discovery", return_value=[]),
            mock.patch.object(cli, "OpenAICompatibleProvider", provider_cls),
        ]
        if engine_error is not None:
            patches.append(
                mock.patch.object(
                    cli, "load_engine", side_effect=engine_error
                )
            )

        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, FULL_ENV, clear=True):
            for patcher in patches:
                patcher.start()
            try:
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    status = cli.main(argv)
            finally:
                for patcher in reversed(patches):
                    patcher.stop()
        return status, stdout.getvalue(), stderr.getvalue()

    def _yellow_item(self):
        return FakeItem(
            "item-1",
            "adhoc-tool",
            r"C:\Users\alice\AppData\Local\MysteryTempDir",
            "AppData/Local/MysteryTempDir",
        )

    def _json_payload(self, stdout):
        return json.loads(stdout)

    @staticmethod
    def _request_count():
        return sum(len(provider.requests) for provider in FakeProvider.instances)


class DefaultOffTests(CliTestCase):
    def test_json_output_is_unchanged_without_ai_flag(self):
        status, stdout, stderr = self._run_main(["--json"], [])
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        payload = self._json_payload(stdout)
        self.assertEqual(set(payload), {"summary", "tools", "warnings"})

    def test_ai_without_sizes_prints_one_hint(self):
        status, stdout, stderr = self._run_main(["--json", "--ai"], [])
        self.assertEqual(status, 0)
        self.assertEqual(stderr.count(cli.AI_HINT_NEEDS_SIZES), 1)
        self.assertNotIn("ai", self._json_payload(stdout))

    def test_ai_without_key_prints_one_hint_and_keeps_output(self):
        env = {
            key: value
            for key, value in FULL_ENV.items()
            if not key.startswith("DISK_WARDEN_AI_")
        }
        report = FakeSizeReport([self._yellow_item()])
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(cli, "discover", return_value=FakeDiscovery()), \
                    mock.patch.object(cli, "measure", return_value=report), \
                    mock.patch.object(cli, "targets_from_discovery", return_value=[]):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    status = cli.main(["--json", "--sizes", "--ai"])
        self.assertEqual(status, 0)
        self.assertEqual(stderr.getvalue().count(cli.AI_HINT_MISSING_KEY), 1)
        payload = self._json_payload(stdout.getvalue())
        self.assertNotIn("ai", payload)
        self.assertIn("sizes", payload)


class OptInTests(CliTestCase):
    def test_ai_section_reports_stats_and_confirmable_suggestion(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            cache_path = os.path.join(cache_dir, "ai-cache.json")
            status, stdout, stderr = self._run_main(
                ["--json", "--sizes", "--ai", "--ai-cache", cache_path],
                [self._yellow_item()],
            )
            self.assertEqual(status, 0)
            payload = self._json_payload(stdout)

            self.assertIn("ai", payload)
            ai = payload["ai"]
            self.assertEqual(ai["stats"]["calls"], 1)
            self.assertEqual(ai["stats"]["suggestions"], 1)
            self.assertEqual(ai["stats"]["failures"], 0)
            self.assertEqual(ai["summary"][SAFETY_GREEN], 1)
            self.assertEqual(len(ai["suggestions"]), 1)

            suggestion = ai["suggestions"][0]
            self.assertEqual(suggestion["safety"], SAFETY_GREEN)
            self.assertEqual(suggestion["source"], SOURCE_AI)
            self.assertTrue(suggestion["requires_confirm"])
            self.assertFalse(suggestion["can_migrate"])
            self.assertEqual(suggestion["tool_id"], "adhoc-tool")

            self.assertNotIn("sk-test-secret", stdout)
            self.assertNotIn("sk-test-secret", stderr)
            ai_text = json.dumps(ai, ensure_ascii=False)
            self.assertNotIn("alice", ai_text)
            self.assertNotIn(r"C:\Users\alice", ai_text)

            sent = FakeProvider.instances[0].requests[0]
            user_message = sent["messages"][1]["content"]
            self.assertNotIn("alice", user_message)
            self.assertNotIn(r"C:\Users", user_message)
            self.assertIn("MysteryTempDir", user_message)

            self.assertTrue(os.path.exists(cache_path))
            with open(cache_path, "r", encoding="utf-8") as handle:
                stored = json.load(handle)
            self.assertEqual(len(stored["entries"]), 1)

    def test_cache_hit_does_not_call_the_provider_again(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            cache_path = os.path.join(cache_dir, "ai-cache.json")
            args = ["--json", "--sizes", "--ai", "--ai-cache", cache_path]

            first_status, _first_out, _ = self._run_main(
                args, [self._yellow_item()]
            )
            self.assertEqual(first_status, 0)
            self.assertEqual(self._request_count(), 1)

            second_status, second_out, _ = self._run_main(
                args, [self._yellow_item()]
            )
            self.assertEqual(second_status, 0)
            self.assertEqual(self._request_count(), 1)

            stats = self._json_payload(second_out)["ai"]["stats"]
            self.assertEqual(stats["calls"], 0)
            self.assertEqual(stats["cache_hits"], 1)
            self.assertEqual(stats["suggestions"], 1)

    def test_red_items_never_reach_the_provider(self):
        login = FakeItem(
            "item-red",
            "adhoc-tool",
            r"C:\Users\alice\AppData\Local\Google\Chrome\User Data\Default\Login Data",
            "AppData/Local/Google/Chrome/User Data/Default/Login Data",
            kind="file",
        )
        with tempfile.TemporaryDirectory() as cache_dir:
            cache_path = os.path.join(cache_dir, "ai-cache.json")
            status, stdout, _ = self._run_main(
                ["--json", "--sizes", "--ai", "--ai-cache", cache_path],
                [login],
            )
        self.assertEqual(status, 0)
        payload = self._json_payload(stdout)
        self.assertEqual(payload["ai"]["stats"]["calls"], 0)
        self.assertEqual(payload["ai"]["summary"][SAFETY_RED], 1)
        self.assertEqual(payload["ai"]["suggestions"], [])
        self.assertEqual(FakeProvider.instances[0].requests, [])

    def test_rules_failure_keeps_the_original_output(self):
        status, stdout, stderr = self._run_main(
            ["--json", "--sizes", "--ai"],
            [self._yellow_item()],
            engine_error=RulesError("rules are broken"),
        )
        self.assertEqual(status, 0)
        payload = self._json_payload(stdout)
        self.assertNotIn("ai", payload)
        self.assertIn("rules error: rules are broken", stderr)

    def test_human_output_marks_suggestions_as_requiring_confirmation(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            cache_path = os.path.join(cache_dir, "ai-cache.json")
            status, stdout, _ = self._run_main(
                ["--sizes", "--ai", "--ai-cache", cache_path],
                [self._yellow_item()],
            )
        self.assertEqual(status, 0)
        self.assertIn("ai: 1 calls, 0 cache hits, 1 suggestions", stdout)
        self.assertIn("MysteryTempDir", stdout)
        self.assertIn("(requires confirmation)", stdout)


class ResolverConstructionTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(CliTestCase._clear_ai_logger)

    def _args(self, **overrides):
        values = {
            "ai": True,
            "ai_cache": None,
            "rules": str(cli.DEFAULT_RULES),
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_missing_key_returns_no_resolver(self):
        env = {"USERNAME": "alice", "USERPROFILE": r"C:\Users\alice"}
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True):
            with redirect_stderr(stderr):
                resolver, config = cli._build_ai_resolver(self._args(), os.environ)
        self.assertIsNone(resolver)
        self.assertTrue(config.enabled)
        self.assertFalse(config.available)
        self.assertEqual(config.disabled_reason, "missing_api_key")
        self.assertEqual(stderr.getvalue().count(cli.AI_HINT_MISSING_KEY), 1)

    def test_cache_override_is_applied(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            cache_path = os.path.join(cache_dir, "custom.json")
            with mock.patch.dict(os.environ, FULL_ENV, clear=True):
                with mock.patch.object(
                    cli, "OpenAICompatibleProvider", FakeProvider
                ):
                    resolver, config = cli._build_ai_resolver(
                        self._args(ai_cache=cache_path), os.environ
                    )
            self.assertIsNotNone(resolver)
            self.assertEqual(config.cache_path, cache_path)
            self.assertEqual(config.model, "test-model")
            self.assertEqual(resolver.cache.path, cache_path)
            self.assertNotIn("sk-test-secret", repr(config))
            self.assertNotIn("sk-test-secret", repr(resolver))


if __name__ == "__main__":
    unittest.main()
