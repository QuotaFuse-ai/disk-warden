# -*- coding: utf-8 -*-
"""Configuration for the optional third-layer AI analysis (T22).

The AI layer is **off by default**.  It is only usable when all of the
following hold:

* the caller (CLI ``--ai`` or the settings switch) enabled it;
* ``DISK_WARDEN_AI_API_KEY`` (or the settings equivalent) is present;
* a base URL and a model name are configured.

Environment variables (all optional unless stated):

======================  ==============================================
``DISK_WARDEN_AI_ENABLED``       ``1/true/yes/on`` enables the layer
``DISK_WARDEN_AI_BASE_URL``      OpenAI-compatible endpoint, e.g.
                                 ``https://host/v1``
``DISK_WARDEN_AI_MODEL``         model name
``DISK_WARDEN_AI_API_KEY``       **required** for the layer to be available
``DISK_WARDEN_AI_MAX_CALLS``     network call cap per resolver (default 20)
``DISK_WARDEN_AI_MAX_TOKENS``    estimated token cap per resolver
                                 (default 60000, ``0`` disables)
``DISK_WARDEN_AI_MAX_OUTPUT_TOKENS`` per-request output cap (default 200)
``DISK_WARDEN_AI_TIMEOUT``       per-request timeout in seconds (default 10)
``DISK_WARDEN_AI_CACHE``         cache file path (default under
                                 ``%LOCALAPPDATA%`` / ``$XDG_CACHE_HOME``)
``DISK_WARDEN_AI_CACHE_TTL``     cache lifetime in seconds (default 30 days)
``DISK_WARDEN_AI_MAX_PATH_SEGMENTS`` sanitized path fragments sent per item
                                 (default 3, hard cap 6)
======================  ==============================================

The API key is never part of ``repr()`` / ``public_dict()`` and must not be
written to logs or the repository.

Python 3.8 compatible (decision 0005).
"""

import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

ENV_ENABLED = "DISK_WARDEN_AI_ENABLED"
ENV_BASE_URL = "DISK_WARDEN_AI_BASE_URL"
ENV_MODEL = "DISK_WARDEN_AI_MODEL"
ENV_API_KEY = "DISK_WARDEN_AI_API_KEY"
ENV_MAX_CALLS = "DISK_WARDEN_AI_MAX_CALLS"
ENV_MAX_TOKENS = "DISK_WARDEN_AI_MAX_TOKENS"
ENV_MAX_OUTPUT_TOKENS = "DISK_WARDEN_AI_MAX_OUTPUT_TOKENS"
ENV_TIMEOUT = "DISK_WARDEN_AI_TIMEOUT"
ENV_CACHE = "DISK_WARDEN_AI_CACHE"
ENV_CACHE_TTL = "DISK_WARDEN_AI_CACHE_TTL"
ENV_MAX_PATH_SEGMENTS = "DISK_WARDEN_AI_MAX_PATH_SEGMENTS"

DEFAULT_MAX_CALLS = 20
DEFAULT_MAX_TOTAL_TOKENS = 60000
DEFAULT_MAX_OUTPUT_TOKENS = 200
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_CACHE_TTL_SECONDS = 30 * 24 * 60 * 60
DEFAULT_MAX_PATH_SEGMENTS = 3
MAX_PATH_SEGMENTS_LIMIT = 6

REASON_DISABLED = "disabled"
REASON_MISSING_KEY = "missing_api_key"
REASON_MISSING_BASE_URL = "missing_base_url"
REASON_MISSING_MODEL = "missing_model"

TRUE_VALUES = frozenset(("1", "true", "yes", "on"))
FALSE_VALUES = frozenset(("0", "false", "no", "off", ""))


@dataclass(frozen=True)
class AIConfig:
    """Immutable snapshot of the AI layer settings.

    ``api_key`` is excluded from ``repr`` and ``public_dict`` on purpose.
    """

    enabled: bool = False
    base_url: str = ""
    model: str = ""
    api_key: str = field(default="", repr=False)
    max_calls: int = DEFAULT_MAX_CALLS
    max_total_tokens: int = DEFAULT_MAX_TOTAL_TOKENS
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS
    max_path_segments: int = DEFAULT_MAX_PATH_SEGMENTS
    cache_path: str = ""

    @property
    def disabled_reason(self):
        # type: () -> str
        """Empty string when usable, otherwise a stable reason code."""
        if not self.enabled:
            return REASON_DISABLED
        if not self.api_key:
            return REASON_MISSING_KEY
        if not self.base_url:
            return REASON_MISSING_BASE_URL
        if not self.model:
            return REASON_MISSING_MODEL
        return ""

    @property
    def available(self):
        # type: () -> bool
        return not self.disabled_reason

    def public_dict(self):
        # type: () -> dict
        """Safe-to-print view; never contains the API key."""
        return {
            "enabled": self.enabled,
            "available": self.available,
            "disabled_reason": self.disabled_reason,
            "base_url": self.base_url,
            "model": self.model,
            "max_calls": self.max_calls,
            "max_total_tokens": self.max_total_tokens,
            "max_output_tokens": self.max_output_tokens,
            "timeout_seconds": self.timeout_seconds,
            "max_path_segments": self.max_path_segments,
            "cache_enabled": bool(self.cache_path),
        }


def _truthy(raw):
    # type: (object) -> bool
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return False
    return str(raw).strip().casefold() in TRUE_VALUES


def _pick(environ, settings, env_name, setting_key, default):
    # type: (Mapping[str, str], Mapping[str, Any], str, str, Any) -> Any
    if env_name in environ and str(environ[env_name]).strip() != "":
        return environ[env_name]
    if settings and settings.get(setting_key) not in (None, ""):
        return settings.get(setting_key)
    return default


def _as_int(raw, default, minimum=0, maximum=None):
    # type: (object, int, int, Optional[int]) -> int
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if value < minimum:
        return minimum
    if maximum is not None and value > maximum:
        return maximum
    return value


def _as_float(raw, default, minimum=0.1):
    # type: (object, float, float) -> float
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if value < minimum:
        return minimum
    return value


def _as_text(raw):
    # type: (object) -> str
    if not isinstance(raw, str):
        return ""
    return raw.strip()


def default_cache_path(environ=None):
    # type: (Optional[Mapping[str, str]]) -> str
    """Per-user cache location; empty string when no home can be determined."""
    env = environ if environ is not None else os.environ
    if os.name == "nt":
        base = env.get("LOCALAPPDATA") or env.get("APPDATA")
        if base:
            return os.path.join(base, "disk-warden", "ai-cache.json")
        return ""
    base = env.get("XDG_CACHE_HOME")
    if base:
        return os.path.join(base, "disk-warden", "ai-cache.json")
    home = env.get("HOME")
    if home:
        return os.path.join(home, ".cache", "disk-warden", "ai-cache.json")
    return ""


def load_config(environ=None, settings=None, enabled=None):
    # type: (Optional[Mapping[str, str]], Optional[Mapping[str, Any]], Optional[bool]) -> AIConfig
    """Build an :class:`AIConfig` from settings and environment.

    ``enabled`` is the explicit caller decision (CLI ``--ai``).  When it is
    ``None`` the settings switch and ``DISK_WARDEN_AI_ENABLED`` decide.  The
    default is always **off**.
    """
    env = environ if environ is not None else os.environ
    settings = settings or {}

    if enabled is None:
        raw_enabled = _pick(
            env, settings, ENV_ENABLED, "enabled", settings.get("enabled")
        )
        enabled_flag = _truthy(raw_enabled)
    else:
        enabled_flag = bool(enabled)

    cache_path = _as_text(
        _pick(env, settings, ENV_CACHE, "cache_path", "")
    )
    if not cache_path and enabled_flag:
        cache_path = _as_text(settings.get("cache_path")) or default_cache_path(env)

    return AIConfig(
        enabled=enabled_flag,
        base_url=_as_text(_pick(env, settings, ENV_BASE_URL, "base_url", "")),
        model=_as_text(_pick(env, settings, ENV_MODEL, "model", "")),
        api_key=_as_text(_pick(env, settings, ENV_API_KEY, "api_key", "")),
        max_calls=_as_int(
            _pick(env, settings, ENV_MAX_CALLS, "max_calls", DEFAULT_MAX_CALLS),
            DEFAULT_MAX_CALLS,
            minimum=0,
            maximum=10000,
        ),
        max_total_tokens=_as_int(
            _pick(
                env,
                settings,
                ENV_MAX_TOKENS,
                "max_total_tokens",
                DEFAULT_MAX_TOTAL_TOKENS,
            ),
            DEFAULT_MAX_TOTAL_TOKENS,
            minimum=0,
        ),
        max_output_tokens=_as_int(
            _pick(
                env,
                settings,
                ENV_MAX_OUTPUT_TOKENS,
                "max_output_tokens",
                DEFAULT_MAX_OUTPUT_TOKENS,
            ),
            DEFAULT_MAX_OUTPUT_TOKENS,
            minimum=1,
            maximum=4096,
        ),
        timeout_seconds=_as_float(
            _pick(env, settings, ENV_TIMEOUT, "timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
            DEFAULT_TIMEOUT_SECONDS,
        ),
        ttl_seconds=_as_int(
            _pick(env, settings, ENV_CACHE_TTL, "ttl_seconds", DEFAULT_CACHE_TTL_SECONDS),
            DEFAULT_CACHE_TTL_SECONDS,
            minimum=1,
        ),
        max_path_segments=_as_int(
            _pick(
                env,
                settings,
                ENV_MAX_PATH_SEGMENTS,
                "max_path_segments",
                DEFAULT_MAX_PATH_SEGMENTS,
            ),
            DEFAULT_MAX_PATH_SEGMENTS,
            minimum=1,
            maximum=MAX_PATH_SEGMENTS_LIMIT,
        ),
        cache_path=cache_path,
    )


__all__ = [
    "AIConfig",
    "DEFAULT_MAX_CALLS",
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "DEFAULT_MAX_TOTAL_TOKENS",
    "DEFAULT_MAX_PATH_SEGMENTS",
    "DEFAULT_CACHE_TTL_SECONDS",
    "MAX_PATH_SEGMENTS_LIMIT",
    "REASON_DISABLED",
    "REASON_MISSING_BASE_URL",
    "REASON_MISSING_KEY",
    "REASON_MISSING_MODEL",
    "default_cache_path",
    "load_config",
]
