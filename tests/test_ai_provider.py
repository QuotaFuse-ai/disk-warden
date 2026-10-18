# -*- coding: utf-8 -*-
"""Transport tests for the OpenAI-compatible provider (T22).

Every test injects a fake opener; no test touches the network.
"""

import io
import json
import socket
import unittest
import urllib.error

from src.core.ai.provider import (
    CODE_HTTP,
    CODE_INVALID_RESPONSE,
    CODE_NETWORK,
    CODE_RATE_LIMITED,
    CODE_TIMEOUT,
    OpenAICompatibleProvider,
    ProviderError,
    endpoint_for,
)


class FakeResponse(object):
    def __init__(self, body):
        self._body = body
        self.closed = False

    def read(self, size=-1):
        if size is None or size < 0:
            return self._body
        return self._body[:size]

    def close(self):
        self.closed = True


class FakeOpener(object):
    def __init__(self, result):
        self.result = result
        self.requests = []
        self.timeouts = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        if isinstance(self.result, BaseException):
            raise self.result
        return FakeResponse(self.result)


def ok_body():
    return json.dumps(
        {"choices": [{"message": {"content": '{"safety":"green","reason":"cache"}'}}]}
    ).encode("utf-8")


def make_provider(opener, **overrides):
    kwargs = {
        "base_url": "https://api.example.test/v1",
        "model": "test-model",
        "api_key": "sk-test-secret",
        "timeout_seconds": 5,
        "opener": opener,
    }
    kwargs.update(overrides)
    return OpenAICompatibleProvider(**kwargs)


class EndpointTests(unittest.TestCase):
    def test_appends_chat_completions(self):
        self.assertEqual(
            endpoint_for("https://api.example.test/v1"),
            "https://api.example.test/v1/chat/completions",
        )

    def test_keeps_existing_suffix_and_strips_slash(self):
        self.assertEqual(
            endpoint_for("https://api.example.test/v1/chat/completions/"),
            "https://api.example.test/v1/chat/completions",
        )

    def test_empty_base_url_is_rejected(self):
        with self.assertRaises(ValueError):
            endpoint_for("")


class ConstructionTests(unittest.TestCase):
    def test_key_and_model_are_required(self):
        with self.assertRaises(ValueError):
            OpenAICompatibleProvider("https://x", "m", "")
        with self.assertRaises(ValueError):
            OpenAICompatibleProvider("https://x", "", "sk-test-secret")

    def test_repr_never_contains_the_key(self):
        provider = make_provider(FakeOpener(ok_body()))
        self.assertNotIn("sk-test-secret", repr(provider))
        self.assertIn("test-model", repr(provider))


class RequestTests(unittest.TestCase):
    def test_posts_json_to_the_chat_endpoint(self):
        opener = FakeOpener(ok_body())
        provider = make_provider(opener)
        response = provider.classify({"model": "test-model", "messages": []})

        self.assertEqual(len(opener.requests), 1)
        request = opener.requests[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(
            request.full_url,
            "https://api.example.test/v1/chat/completions",
        )
        self.assertEqual(
            request.get_header("Authorization"), "Bearer sk-test-secret"
        )
        self.assertIn("application/json", request.get_header("Content-type"))
        self.assertEqual(opener.timeouts, [5.0])
        self.assertEqual(response["choices"][0]["message"]["content"],
                         '{"safety":"green","reason":"cache"}')

    def test_body_is_the_given_payload(self):
        opener = FakeOpener(ok_body())
        provider = make_provider(opener)
        payload = {"model": "test-model", "temperature": 0, "messages": []}
        provider.classify(payload)
        self.assertEqual(
            json.loads(opener.requests[0].data.decode("utf-8")), payload
        )


class FailureTests(unittest.TestCase):
    def _assert_code(self, result, code):
        opener = FakeOpener(result)
        provider = make_provider(opener)
        with self.assertRaises(ProviderError) as caught:
            provider.classify({})
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(len(opener.requests), 1)  # never retried
        return caught.exception

    def test_rate_limit(self):
        error = urllib.error.HTTPError(
            "https://x", 429, "too many requests", None, io.BytesIO(b"")
        )
        raised = self._assert_code(error, CODE_RATE_LIMITED)
        self.assertEqual(raised.status, 429)

    def test_http_error(self):
        error = urllib.error.HTTPError(
            "https://x", 500, "boom", None, io.BytesIO(b"")
        )
        raised = self._assert_code(error, CODE_HTTP)
        self.assertEqual(raised.status, 500)

    def test_socket_timeout(self):
        self._assert_code(socket.timeout("timed out"), CODE_TIMEOUT)

    def test_url_error_with_timeout_reason(self):
        self._assert_code(
            urllib.error.URLError(socket.timeout("timed out")), CODE_TIMEOUT
        )

    def test_url_error_is_network(self):
        self._assert_code(
            urllib.error.URLError(OSError("dns failure")), CODE_NETWORK
        )

    def test_os_error_is_network(self):
        self._assert_code(OSError("connection reset"), CODE_NETWORK)

    def test_invalid_json(self):
        self._assert_code(b"<html>not json</html>", CODE_INVALID_RESPONSE)

    def test_json_array_is_invalid(self):
        self._assert_code(b"[1, 2, 3]", CODE_INVALID_RESPONSE)

    def test_missing_choices_is_invalid(self):
        self._assert_code(b'{"id":"x"}', CODE_INVALID_RESPONSE)

    def test_empty_choices_is_invalid(self):
        self._assert_code(b'{"choices":[]}', CODE_INVALID_RESPONSE)

    def test_oversized_response_is_invalid(self):
        self._assert_code(b"x" * 2048, CODE_INVALID_RESPONSE)

    def test_error_message_never_contains_the_key(self):
        opener = FakeOpener(OSError("connection reset"))
        provider = make_provider(opener)
        with self.assertRaises(ProviderError) as caught:
            provider.classify({})
        self.assertNotIn("sk-test-secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
