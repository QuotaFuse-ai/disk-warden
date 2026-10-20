# -*- coding: utf-8 -*-
"""Optional third-layer AI analysis (T22).

Public API:
    load_config(environ, settings, enabled) -> AIConfig
    build_payload(item, current) -> dict        # sanitized metadata
    OpenAICompatibleProvider(base_url, model, api_key) -> provider
    AICache(path) -> cache
    AIResolver(provider, config, cache, rules_version) -> resolver
    cache_key(sanitized_path, rules_version, prompt_version) -> key

The layer is off by default and only runs for yellow (unknown) items.  Its
suggestions are advisory: ``ClassifyEngine`` always marks them
``requires_confirm=True`` and never lets them override red.

Python 3.8 compatible (decision 0005).
"""

from .cache import (
    AICache,
    CACHE_SCHEMA_VERSION,
    cache_key,
    stable_file_version,
)
from . import keystore
from .config import (
    AIConfig,
    DEFAULT_MAX_CALLS,
    ENV_API_KEY,
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_MAX_TOTAL_TOKENS,
    REASON_DISABLED,
    REASON_MISSING_BASE_URL,
    REASON_MISSING_KEY,
    REASON_MISSING_MODEL,
    default_cache_path,
    load_config,
)
from .payload import (
    DEFAULT_MAX_SEGMENTS,
    PAYLOAD_SCHEMA_VERSION,
    build_payload,
    detect_identity,
    extension_counts,
    has_leak,
    leak_categories,
    sanitize_path,
    sanitize_segments,
    serialize_payload,
)
from .provider import (
    AIProvider,
    CODE_HTTP,
    CODE_INVALID_RESPONSE,
    CODE_NETWORK,
    CODE_RATE_LIMITED,
    CODE_TIMEOUT,
    OpenAICompatibleProvider,
    ProviderError,
)
from .resolver import (
    AIResolver,
    DEFAULT_REASON_KEY,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_request,
    estimate_tokens,
    parse_suggestion,
)

__all__ = [
    "AICache",
    "AIConfig",
    "AIProvider",
    "AIResolver",
    "CACHE_SCHEMA_VERSION",
    "CODE_HTTP",
    "CODE_INVALID_RESPONSE",
    "CODE_NETWORK",
    "CODE_RATE_LIMITED",
    "CODE_TIMEOUT",
    "DEFAULT_MAX_CALLS",
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "DEFAULT_MAX_SEGMENTS",
    "DEFAULT_MAX_TOTAL_TOKENS",
    "DEFAULT_REASON_KEY",
    "ENV_API_KEY",
    "OpenAICompatibleProvider",
    "PAYLOAD_SCHEMA_VERSION",
    "PROMPT_VERSION",
    "ProviderError",
    "REASON_DISABLED",
    "REASON_MISSING_BASE_URL",
    "REASON_MISSING_KEY",
    "REASON_MISSING_MODEL",
    "SYSTEM_PROMPT",
    "build_payload",
    "build_request",
    "cache_key",
    "default_cache_path",
    "detect_identity",
    "estimate_tokens",
    "extension_counts",
    "has_leak",
    "keystore",
    "leak_categories",
    "load_config",
    "parse_suggestion",
    "sanitize_path",
    "sanitize_segments",
    "serialize_payload",
    "stable_file_version",
]
