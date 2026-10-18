# -*- coding: utf-8 -*-
"""OpenAI-compatible HTTP provider for the third-layer AI analysis (T22).

The provider is deliberately small: it POSTs an already-assembled request
payload (the prompt lives in :mod:`src.core.ai.resolver`) and returns the
decoded response envelope.  It never reads files, never logs headers or the
API key, and never retries — retrying would make the per-scan call budget
unpredictable.  Every failure is reported as a :class:`ProviderError` with a
stable code so the resolver can degrade to yellow.

The opener is injectable (``opener=...``) so unit tests never touch a real
network.

Python 3.8 compatible (decision 0005).
"""

import contextlib
import json
import socket
import urllib.error
import urllib.request

try:  # Python 3.8+
    from typing import Protocol
except ImportError:  # pragma: no cover - only for very old interpreters
    Protocol = object  # type: ignore

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024
MAX_TIMEOUT_SECONDS = 120.0

CODE_TIMEOUT = "timeout"
CODE_NETWORK = "network"
CODE_HTTP = "http"
CODE_RATE_LIMITED = "rate_limited"
CODE_INVALID_RESPONSE = "invalid_response"

ERROR_CODES = (
    CODE_TIMEOUT,
    CODE_NETWORK,
    CODE_HTTP,
    CODE_RATE_LIMITED,
    CODE_INVALID_RESPONSE,
)


class ProviderError(Exception):
    """A non-fatal transport or protocol failure."""

    def __init__(self, code, message="", status=None):
        # type: (str, str, object) -> None
        self.code = code
        self.status = status
        text = "[%s]" % code
        if message:
            text = "%s %s" % (text, message)
        Exception.__init__(self, text)


class AIProvider(Protocol):
    """Contract consumed by :class:`src.core.ai.resolver.AIResolver`."""

    def classify(self, payload):
        # type: (dict) -> object
        """Send one request and return the decoded JSON envelope.

        Raises :class:`ProviderError` for every recoverable failure.
        """
        ...


def endpoint_for(base_url):
    # type: (str) -> str
    """Append ``/chat/completions`` unless the caller already did."""
    url = (base_url or "").strip()
    if not url:
        raise ValueError("base_url is required")
    url = url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url = url + "/chat/completions"
    return url


class OpenAICompatibleProvider(object):
    """Minimal OpenAI chat-completions adapter over ``urllib.request``."""

    def __init__(self, base_url, model, api_key, timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
                 opener=None, max_response_bytes=DEFAULT_MAX_RESPONSE_BYTES):
        # type: (str, str, str, float, object, int) -> None
        if not api_key:
            raise ValueError("api_key is required")
        if not model:
            raise ValueError("model is required")
        self.url = endpoint_for(base_url)
        self.model = model
        self._api_key = api_key
        self.timeout_seconds = max(0.1, min(float(timeout_seconds), MAX_TIMEOUT_SECONDS))
        self.max_response_bytes = max(1024, int(max_response_bytes))
        self._opener = opener if opener is not None else urllib.request.build_opener()

    def __repr__(self):
        # type: () -> str
        return "OpenAICompatibleProvider(url=%r, model=%r, timeout=%ss)" % (
            self.url,
            self.model,
            self.timeout_seconds,
        )

    def classify(self, payload):
        # type: (dict) -> dict
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": "Bearer %s" % self._api_key,
            },
        )

        try:
            with contextlib.closing(
                self._opener.open(request, timeout=self.timeout_seconds)
            ) as response:
                raw = response.read(self.max_response_bytes + 1)
        except urllib.error.HTTPError as exc:
            if getattr(exc, "code", None) == 429:
                raise ProviderError(
                    CODE_RATE_LIMITED,
                    "provider rate limited the request",
                    status=429,
                )
            raise ProviderError(
                CODE_HTTP,
                "provider returned HTTP %s" % getattr(exc, "code", "?"),
                status=getattr(exc, "code", None),
            )
        except (socket.timeout, TimeoutError):
            raise ProviderError(CODE_TIMEOUT, "request timed out")
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise ProviderError(CODE_TIMEOUT, "request timed out")
            raise ProviderError(CODE_NETWORK, "cannot reach the provider")
        except OSError:
            raise ProviderError(CODE_NETWORK, "network error")

        if len(raw) > self.max_response_bytes:
            raise ProviderError(
                CODE_INVALID_RESPONSE, "response exceeds the size limit"
            )

        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ProviderError(CODE_INVALID_RESPONSE, "response is not valid JSON")

        if not isinstance(data, dict):
            raise ProviderError(
                CODE_INVALID_RESPONSE, "response must be a JSON object"
            )
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderError(
                CODE_INVALID_RESPONSE, "response has no choices"
            )
        return data


__all__ = [
    "AIProvider",
    "CODE_HTTP",
    "CODE_INVALID_RESPONSE",
    "CODE_NETWORK",
    "CODE_RATE_LIMITED",
    "CODE_TIMEOUT",
    "OpenAICompatibleProvider",
    "ProviderError",
    "endpoint_for",
]
