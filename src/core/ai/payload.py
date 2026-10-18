# -*- coding: utf-8 -*-
"""Privacy boundary for the third-layer AI analysis (T22).

Only the following metadata may ever leave the machine:

* up to ``max_segments`` **relative** path fragments (directory names);
* the directory depth;
* file count and total byte size;
* the extension distribution.

Everything else is removed before serialization:

* drive letters and UNC roots (``C:``, ``\\\\host\\share``);
* absolute path prefixes, the home directory and the user name;
* any environment-variable value (``%APPDATA%`` expansions included).

``has_leak()`` / ``leak_categories()`` are the last line of defence: the
resolver refuses to send a payload when any category is still detectable.
The functions in this module never open, read or list a directory; they only
transform metadata already collected by the scan layer.

Python 3.8 compatible (decision 0005).
"""

import json
import os
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

PAYLOAD_SCHEMA_VERSION = 1
DEFAULT_MAX_SEGMENTS = 3
MAX_SEGMENT_LENGTH = 48
MAX_DEPTH = 32
MAX_EXTENSIONS = 12

_SPLIT_RE = re.compile(r"[\\/]+")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_DRIVE_IN_TEXT_RE = re.compile(r"(?:^|[\\/])[A-Za-z]:[\\/]")
_SAFE_EXT_RE = re.compile(r"^[a-z0-9][a-z0-9._+-]{0,11}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_IGNORED_ENV_VALUES = frozenset(("true", "false", "none", "null"))

PLACEHOLDER_USER = "<user>"
PLACEHOLDER_ENV = "<env>"


def _get(item, attribute, default=None):
    # type: (object, str, object) -> object
    if isinstance(item, Mapping):
        return item.get(attribute, default)
    return getattr(item, attribute, default)


def _environment(environ):
    # type: (Optional[Mapping[str, str]]) -> Mapping[str, str]
    return environ if environ is not None else os.environ


def detect_identity(environ=None):
    # type: (Optional[Mapping[str, str]]) -> Tuple[str, str]
    """Return ``(user_name, home_directory)`` using the same env as the caller."""
    env = _environment(environ)
    username = ""
    for key in ("USERNAME", "USER", "LOGNAME"):
        value = env.get(key)
        if isinstance(value, str) and value.strip():
            username = value.strip()
            break
    home = ""
    for key in ("USERPROFILE", "HOME"):
        value = env.get(key)
        if isinstance(value, str) and value.strip():
            home = value.strip().rstrip("\\/")
            break
    return username, home


def _secret_values(environ=None, username=None, home=None):
    # type: (Optional[Mapping[str, str]], Optional[str], Optional[str]) -> List[str]
    """All strings that must never appear in a payload, longest first."""
    env = _environment(environ)
    values = set()

    def add(value):
        # type: (object) -> None
        if isinstance(value, str):
            text = value.strip()
            if len(text) >= 3 and text.casefold() not in _IGNORED_ENV_VALUES:
                values.add(text)

    add(username)
    if home:
        add(home)
        add(os.path.basename(home))

    for value in env.values():
        add(value)

    return sorted(values, key=len, reverse=True)


def _replace_ci(text, needle, replacement):
    # type: (str, str, str) -> str
    if not needle:
        return text
    pattern = re.compile(re.escape(needle), re.IGNORECASE)
    return pattern.sub(replacement, text)


def _strip_absolute_prefix(path, secrets):
    # type: (str, List[str]) -> Tuple[str, bool]
    """Remove UNC host/share, drive and home prefixes; report UNC."""
    text = path.strip()
    unc = text.startswith("\\\\") or text.startswith("//")

    # Longest matching home/environment prefix first (e.g. C:\Users\alice).
    lowered = text.casefold()
    for value in secrets:
        candidate = value.casefold()
        if candidate and lowered.startswith(candidate):
            text = text[len(value):]
            lowered = text.casefold()
            break

    text = _DRIVE_RE.sub("", text)
    return text, unc


def _scrub_segment(segment, secrets, username):
    # type: (str, List[str], str) -> str
    text = segment
    if username:
        text = _replace_ci(text, username, PLACEHOLDER_USER)
    lowered = text.casefold()
    for value in secrets:
        if value.casefold() in lowered:
            text = _replace_ci(text, value, PLACEHOLDER_ENV)
            lowered = text.casefold()
    text = _DRIVE_RE.sub("", text)
    text = _CONTROL_RE.sub("", text)
    text = text.strip(" .\\/")
    return text[:MAX_SEGMENT_LENGTH]


def sanitize_segments(source_path, rel_path=None, environ=None,
                      max_segments=DEFAULT_MAX_SEGMENTS, username=None,
                      home=None):
    # type: (str, Optional[str], Optional[Mapping[str, str]], int, Optional[str], Optional[str]) -> List[str]
    """Return the last ``max_segments`` sanitized relative path fragments.

    ``rel_path`` (relative to the tool root, as produced by the scan layer) is
    preferred; otherwise the absolute ``source_path`` is stripped of its drive,
    UNC root, home directory and environment prefixes before splitting.
    """
    if username is None or home is None:
        detected_user, detected_home = detect_identity(environ)
        username = username if username is not None else detected_user
        home = home if home is not None else detected_home

    secrets = _secret_values(environ, username, home)

    if isinstance(rel_path, str) and rel_path.strip():
        text = rel_path.strip()
        unc = False
        text = _DRIVE_RE.sub("", text)
    else:
        text, unc = _strip_absolute_prefix(source_path or "", secrets)

    segments = [part for part in _SPLIT_RE.split(text) if part not in ("", ".", "..")]

    # \\host\share\dir -> drop host and share; neither is user data but the
    # host name can identify the user's machine.
    if unc and len(segments) >= 2:
        segments = segments[2:]

    cleaned = []  # type: List[str]
    for segment in segments:
        if _DRIVE_RE.match(segment):
            segment = _DRIVE_RE.sub("", segment)
        if not segment:
            continue
        scrubbed = _scrub_segment(segment, secrets, username or "")
        if scrubbed:
            cleaned.append(scrubbed)

    limit = max(1, min(int(max_segments or DEFAULT_MAX_SEGMENTS), 6))
    return cleaned[-limit:]


def sanitize_path(source_path, rel_path=None, environ=None,
                  max_segments=DEFAULT_MAX_SEGMENTS, username=None,
                  home=None):
    # type: (str, Optional[str], Optional[Mapping[str, str]], int, Optional[str], Optional[str]) -> str
    """A single loggable/sendable path fragment, e.g. ``User/workspaceStorage``."""
    return "/".join(
        sanitize_segments(
            source_path,
            rel_path=rel_path,
            environ=environ,
            max_segments=max_segments,
            username=username,
            home=home,
        )
    )


def path_depth(source_path, rel_path=None, environ=None, username=None, home=None):
    # type: (str, Optional[str], Optional[Mapping[str, str]], Optional[str], Optional[str]) -> int
    """Directory depth, capped at :data:`MAX_DEPTH` to bound the payload."""
    if isinstance(rel_path, str) and rel_path.strip():
        text = rel_path.strip()
    else:
        detected_user, detected_home = detect_identity(environ)
        secrets = _secret_values(
            environ,
            username if username is not None else detected_user,
            home if home is not None else detected_home,
        )
        text, _unc = _strip_absolute_prefix(source_path or "", secrets)
    segments = [part for part in _SPLIT_RE.split(text) if part not in ("", ".", "..")]
    return min(len(segments), MAX_DEPTH)


def _normalise_extension(key):
    # type: (object) -> str
    if not isinstance(key, str):
        return "__other__"
    text = key.strip().casefold()
    if not text:
        return "__other__"
    if not text.startswith("."):
        text = "." + text
    if not _SAFE_EXT_RE.match(text.lstrip(".")):
        return "__other__"
    return text[:13]


def extension_counts(item):
    # type: (object) -> Dict[str, int]
    """Top extension distribution; callers may attach ``extensions`` to an item.

    The scan layer does not count extensions yet, so an absent attribute is a
    normal, empty distribution.  Keys are lower-case and dot-prefixed;
    everything past :data:`MAX_EXTENSIONS` is aggregated into ``__other__``.
    """
    raw = None
    for attribute in ("extensions", "extension_counts", "ext_counts"):
        candidate = _get(item, attribute)
        if isinstance(candidate, Mapping):
            raw = candidate
            break
    if not raw:
        return {}

    merged = {}  # type: Dict[str, int]
    for key, value in raw.items():
        try:
            count = int(value)
        except (TypeError, ValueError):
            continue
        if count <= 0:
            continue
        name = _normalise_extension(key)
        merged[name] = merged.get(name, 0) + count

    ordered = sorted(merged.items(), key=lambda pair: (-pair[1], pair[0]))
    kept = ordered[:MAX_EXTENSIONS]
    overflow = sum(count for _name, count in ordered[MAX_EXTENSIONS:])
    result = dict(kept)
    if overflow:
        result["__other__"] = result.get("__other__", 0) + overflow
    return result


def _as_int(value):
    # type: (object) -> int
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def build_payload(item, current=None, max_segments=DEFAULT_MAX_SEGMENTS,
                  environ=None, username=None, home=None):
    # type: (object, object, int, Optional[Mapping[str, str]], Optional[str], Optional[str]) -> Dict[str, object]
    """Assemble the metadata object that is sent to the AI provider.

    ``current`` (the yellow :class:`Classification`) is accepted for signature
    symmetry with the resolver hook but is not sent: rule reasons could embed
    local paths.
    """
    source_path = _get(item, "source_path", "") or ""
    rel_path = _get(item, "rel_path")
    if not isinstance(source_path, str):
        source_path = str(source_path)
    if not isinstance(rel_path, str):
        rel_path = None

    if username is None or home is None:
        detected_user, detected_home = detect_identity(environ)
        username = username if username is not None else detected_user
        home = home if home is not None else detected_home

    segments = sanitize_segments(
        source_path,
        rel_path=rel_path,
        environ=environ,
        max_segments=max_segments,
        username=username,
        home=home,
    )
    return {
        "schema_version": PAYLOAD_SCHEMA_VERSION,
        "path": "/".join(segments),
        "path_segments": list(segments),
        "depth": path_depth(
            source_path,
            rel_path=rel_path,
            environ=environ,
            username=username,
            home=home,
        ),
        "file_count": _as_int(_get(item, "file_count", 0)),
        "total_bytes": _as_int(_get(item, "bytes", _get(item, "total_bytes", 0))),
        "extensions": extension_counts(item),
    }


def serialize_payload(payload):
    # type: (Mapping[str, object]) -> str
    """Deterministic UTF-8-friendly JSON used as the user message."""
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def leak_categories(text, environ=None, username=None, home=None):
    # type: (str, Optional[Mapping[str, str]], Optional[str], Optional[str]) -> List[str]
    """Categories of private data still present in ``text`` (never the values).

    Only category names are returned so the result is safe to log.
    """
    if not isinstance(text, str):
        return []
    lowered = text.casefold()
    found = []  # type: List[str]

    detected_user, detected_home = detect_identity(environ)
    if username is None:
        username = detected_user
    if home is None:
        home = detected_home

    if username and username.casefold() in lowered:
        found.append("username")
    if home and home.casefold() in lowered:
        found.append("home_directory")

    env = _environment(environ)
    for value in env.values():
        if not isinstance(value, str):
            continue
        candidate = value.strip()
        if len(candidate) >= 4 and candidate.casefold() not in _IGNORED_ENV_VALUES:
            if candidate.casefold() in lowered:
                found.append("environment_value")
                break

    if _DRIVE_IN_TEXT_RE.search(text) or re.search(r"(?:^|[\\/])[A-Za-z]:", text):
        found.append("drive_letter")

    return found


def has_leak(text, environ=None, username=None, home=None):
    # type: (str, Optional[Mapping[str, str]], Optional[str], Optional[str]) -> bool
    return bool(leak_categories(text, environ=environ, username=username, home=home))


__all__ = [
    "DEFAULT_MAX_SEGMENTS",
    "MAX_EXTENSIONS",
    "MAX_SEGMENT_LENGTH",
    "PAYLOAD_SCHEMA_VERSION",
    "build_payload",
    "detect_identity",
    "extension_counts",
    "has_leak",
    "leak_categories",
    "path_depth",
    "sanitize_path",
    "sanitize_segments",
    "serialize_payload",
]
