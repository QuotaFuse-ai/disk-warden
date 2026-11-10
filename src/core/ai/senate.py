# -*- coding: utf-8 -*-
"""Bridge between the Disk Warden panel and a StockSenate checkout (QUOT-518).

StockSenate decides how its debate panel is configured: ``config/llm_config.toml``
with a global ``[llm]`` table and one table per debate role under
``[llm.role_llms.<role>]``::

    [llm]
    backend = "openai_compatible"
    backend_url = ""            # must stay empty: non-empty overrides every provider

    [llm.role_llms.market]
    provider = "openai"
    model = "gpt-5"
    api_key_env = "OPENAI_API_KEY"

This module never re-implements StockSenate's rules.  It reads the *live*
``roles.llm`` / ``;schema`` modules from the checkout and calls them, so an
upgrade of StockSenate is picked up without touching Disk Warden:

* :func:`stock_senate_role_names` reads ``roles.llm.ROLE_*`` constants;
* role keys are validated with ``llm.resolve_role_key`` (alias -> canonical,
  warnings preserved) and a ``_check_llm``-based round trip;
* ``schema.validate_config`` runs over the candidate document *before* it is
  written, exactly as StockSenate would check it on reload.

Writing keeps every comment, blank line and ordering in the file.  The file is
edited structurally (only the ``provider`` / ``model`` / ``api_key_env`` keys of
the touched role tables change, missing tables are appended) rather than
re-serialised, and then replaced atomically via
:func:`src.core.store.atomic.atomic_write_bytes`.  A ``.bak`` copy of the
previous file sits next to it for the operator.

Secrets never reach the TOML: ``api_key_env`` stores the *name* of an
environment variable, and keys that look like literal credentials
(``api_key``, ``token``, ``secret``, ``password``... ) are rejected outright.
Global ``[llm]`` values that could carry a secret (``backend_url`` with an
embedded user:password) are likewise refused.

Python 3.8 compatible (decision 0005); uses ``tomllib`` (3.11+) when present
and falls back to a small structural reader otherwise.
"""

import io
import logging
import os
import re
import threading

from ..store.atomic import StoreError, atomic_write_bytes, utc_now

try:  # Python 3.11+
    import tomllib as _tomllib
except ImportError:  # pragma: no cover - the shipped runtime is 3.12
    _tomllib = None

LOGGER = logging.getLogger("disk_warden.core.ai.senate")

CONFIG_RELATIVE_PATH = os.path.join("config", "llm_config.toml")
SCRIPT_MARKER = ".stock_senate.marker"
AUDIT_FILE_NAME = "senate-role-models.log"
BACKUP_SUFFIX = ".bak"

MAX_ROLES = 64
MAX_VALUE_LENGTH = 200
MAX_FILE_BYTES = 512 * 1024
MAX_AUDIT_LINES = 2000

#: StockSenate provider names, mirroring ``roles/llm.py``'s client dicts.
#: The checkout stays authoritative; this list is the fallback when the module
#: cannot be loaded (and the authored model suggestions below are always ours).
PROVIDER_NAMES = (
    "openai",
    "azure",
    "anthropic",
    "google",
    "openrouter",
    "deepseek",
    "ollama",
    "lmstudio",
    "custom",
)

#: Provider -> conventional environment variable holding the API key.  Mirrors
#: ``_PROVIDER_KEY_ENV`` in ``roles/llm.py`` *plus* the trap it documents:
#: ``anthropic`` is not in that map and would fall back to ``OPENAI_API_KEY``,
#: so it must be given an explicit ``api_key_env``.  Same for providers that
#: only make sense against a local server (``ollama``, ``lmstudio``).
SUGGESTED_KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "azure": "AZURE_OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "custom": "STOCKSENATE_CUSTOM_API_KEY",
}

#: Authored model suggestions; StockSenate has no catalog and accepts any
#: free-string model name, so this list only powers the panel's dropdown and
#: the field stays free-text.
MODEL_SUGGESTIONS = {
    "openai": ("gpt-5", "gpt-5-mini", "gpt-4.1", "gpt-4o", "o4-mini"),
    "azure": ("gpt-4o", "gpt-4.1", "gpt-5"),
    "anthropic": (
        "claude-opus-4-1",
        "claude-sonnet-4-5",
        "claude-haiku-4-5",
    ),
    "google": ("gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"),
    "openrouter": (
        "anthropic/claude-sonnet-4.5",
        "openai/gpt-5",
        "google/gemini-2.5-pro",
        "deepseek/deepseek-chat",
    ),
    "deepseek": ("deepseek-chat", "deepseek-reasoner"),
    "ollama": ("llama3.2", "qwen2.5", "deepseek-r1"),
    "lmstudio": ("local-model",),
    "custom": (),
}

ROLE_KEYS_EDITED = ("provider", "model", "api_key_env")

#: Substrings that mark a TOML key as carrying a literal credential.  Only
#: ``*_env`` names are allowed to mention them.
_SECRET_KEY = re.compile(
    r"(api[_-]?key|secret|token|password|passwd|credential|private[_-]?key)",
    re.IGNORECASE,
)
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_ROLE_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PROVIDER = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_USERINFO_URL = re.compile(r"^[a-z][a-z0-9+.-]*://[^/@\s]*:[^/@\s]*@", re.IGNORECASE)


class SenateError(RuntimeError):
    """A bridge failure whose ``code`` maps onto a bridge error code."""

    def __init__(self, code, reason, detail=None):
        # type: (str, str, object) -> None
        super(SenateError, self).__init__(reason)
        self.code = code
        self.reason = reason
        self.detail = dict(detail) if isinstance(detail, dict) else {}


# ---------------------------------------------------------------------------
# Locating the checkout and loading StockSenate's own rules
# ---------------------------------------------------------------------------


def locate_config(environ=None, explicit=None):
    # type: (object, object) -> str
    """Return the absolute ``llm_config.toml`` path, or when the checkout is
    only known by root directory, the config path under that root."""
    if explicit:
        return os.path.abspath(os.path.normpath(str(explicit)))
    environ = environ if environ is not None else os.environ
    override = (environ.get("STOCKSENATE_CONFIG") or "").strip()
    if override:
        return os.path.abspath(os.path.normpath(override))
    for name in ("STOCKSENATE_ROOT", "STOCKSENATE_HOME"):
        root = (environ.get(name) or "").strip()
        if root:
            return os.path.abspath(
                os.path.normpath(os.path.join(root, CONFIG_RELATIVE_PATH))
            )
    raise SenateError(
        "SENATE_NOT_CONFIGURED",
        "StockSenate config path is not configured",
        {"hint": "set STOCKSENATE_CONFIG or STOCKSENATE_ROOT"},
    )


def _load_module(name, path):
    # type: (str, str) -> object
    import sys
    import types

    module = types.ModuleType(name)
    module.__file__ = path
    with open(path, "rb") as handle:
        source = handle.read()
    previous = sys.modules.get(name)
    sys.modules[name] = module
    try:
        exec(compile(source, path, "exec"), module.__dict__)
    except Exception:
        if previous is not None:
            sys.modules[name] = previous
        else:
            sys.modules.pop(name, None)
        raise
    return module


class SenateRules(object):
    """StockSenate's own role/provider rules, loaded from the checkout."""

    def __init__(self, roles_dir, llm, schema):
        # type: (str, object, object) -> None
        self.roles_dir = roles_dir
        self.llm = llm
        self.schema = schema
        self.role_names = tuple(sorted(_role_constants(llm))) or (
            "market",
            "fundamentals",
            "news",
            "social",
            "bull",
            "bear",
            "risk",
            "technical",
            "macro",
            "valuation",
            "sentiment",
            "flow",
            "policy",
            "competition",
            "management",
            "lockup",
        )
        self.alias_map = _alias_map(llm)
        self.providers = _providers(llm)
        self.defaults = _llm_defaults(llm, schema)

    # -- validation -------------------------------------------------------

    def canonical_role(self, key):
        # type: (str) -> object
        """Return ``(canonical, warning_or_empty)`` or raise on a bad key."""
        if not isinstance(key, str) or not key:
            raise SenateError(
                "SENATE_INVALID_ROLE", "role key must be a non-empty string"
            )
        if key in self.role_names:
            return key, ""
        if key in self.alias_map:
            return self.alias_map[key], "alias:%s->%s" % (key, self.alias_map[key])
        resolver = getattr(self.llm, "resolve_role_key", None)
        if callable(resolver):
            try:
                resolved = resolver(key)
            except Exception:
                resolved = None
            if isinstance(resolved, str) and resolved in self.role_names:
                warning = "" if resolved == key else "alias:%s->%s" % (key, resolved)
                return resolved, warning
        raise SenateError(
            "SENATE_INVALID_ROLE", "unknown debate role: %s" % key, {"role": key}
        )

    def validate_document(self, document):
        # type: (dict) -> object
        """Run StockSenate's ``validate_config`` and return the llm entry."""
        validator = getattr(self.schema, "validate_config", None)
        if not callable(validator):
            raise SenateError(
                "SENATE_VALIDATION", "StockSenate schema.validate_config is missing"
            )
        try:
            result = validator(document)
        except Exception as exc:
            raise SenateError("SENATE_VALIDATION", str(exc) or "invalid config")
        errors = getattr(result, "errors", None)
        if errors:
            raise SenateError(
                "SENATE_VALIDATION",
                "; ".join(str(item) for item in errors),
                {"errors": [str(item) for item in errors]},
            )
        config = getattr(result, "config", None)
        llm = getattr(config, "llm", None) if config is not None else None
        if llm is None:
            raise SenateError("SENATE_VALIDATION", "config has no [llm] section")
        warnings = getattr(result, "warnings", None) or []
        return llm, [str(item) for item in warnings]

    def defaults_toml(self):
        # type: () -> str
        """``config/llm_config.toml`` as written by StockSenate itself."""
        exporter = getattr(self.llm, "default_llm_config_toml", None)
        if callable(exporter):
            text = exporter()
            if isinstance(text, str) and text.strip():
                return text
        lines = [
            "# Written by Disk Warden for StockSenate (QUOT-518).",
            "# Edit role models in the Disk Warden panel, not here.",
            "",
            "[llm]",
            'backend = "%s"' % self.defaults.get("backend", "openai_compatible"),
            'backend_url = ""',
            "",
            "# Secrets stay in the environment (.env): api_key_env names a",
            "# variable, it never holds the key itself.",
        ]
        for role in self.role_names:
            lines += [
                "",
                "[llm.role_llms.%s]" % role,
                'provider = "openai"',
                'model = "%s"' % _DEFAULT_MODEL,
                'api_key_env = "OPENAI_API_KEY"',
            ]
        return "\n".join(lines) + "\n"


def load_rules(roles_dir):
    # type: (str) -> SenateRules
    llm_path = os.path.join(roles_dir, "llm.py")
    schema_path = os.path.join(roles_dir, "schema.py")
    try:
        llm = _load_module("_dw_stocksenate_llm", llm_path)
        schema = _load_module("_dw_stocksenate_schema", schema_path)
    except (OSError, SyntaxError) as exc:
        raise SenateError(
            "SENATE_NOT_CONFIGURED",
            "StockSenate roles module could not be loaded: %s" % exc,
            {"roles_dir": roles_dir},
        )
    except Exception as exc:  # import-time failure inside StockSenate
        raise SenateError(
            "SENATE_NOT_CONFIGURED",
            "StockSenate roles module failed to import: %s" % exc,
            {"roles_dir": roles_dir},
        )
    return SenateRules(roles_dir, llm, schema)


def roles_dir_for(config_path):
    # type: (str) -> str
    root = os.path.dirname(os.path.dirname(os.path.abspath(config_path)))
    return os.path.join(root, "roles")


_DEFAULT_MODEL = "gpt-5"


def _role_constants(llm):
    names = []
    for name, value in vars(llm).items():
        if name.startswith("ROLE_") and isinstance(value, str) and value:
            canonical = _canonical_name(name[len("ROLE_"):])
            if canonical:
                names.append(canonical)
    return names


def _canonical_name(upper):
    # type: (str) -> str
    lower = upper.lower()
    for role in ("market", "fundamentals", "news", "social", "bull", "bear"):
        if lower.startswith(role):
            return role
    return lower


def _alias_map(llm):
    aliases = {}
    for name in ("ROLE_ALIASES", "_ROLE_ALIASES"):
        table = getattr(llm, name, None)
        if isinstance(table, dict):
            for alias, target in table.items():
                if isinstance(alias, str) and isinstance(target, str):
                    aliases[alias] = target
            break
    getter = getattr(llm, "alias_map", None)
    if callable(getter) and not aliases:
        try:
            table = getter()
            if isinstance(table, dict):
                aliases.update(
                    (k, v)
                    for k, v in table.items()
                    if isinstance(k, str) and isinstance(v, str)
                )
        except Exception:
            pass
    return aliases


def _providers(llm):
    found = set()

    def walk(value, depth=0):
        if depth > 3:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(key, str) and _PROVIDER.match(key):
                    found.add(key)
                walk(item, depth + 1)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item, depth + 1)

    for name in ("_CLIENT_CACHE", "_clients", "_PROVIDER_KEY_ENV", "PROVIDER_KEY_ENV",
                 "PROVIDERS", "_PROVIDERS", "create_client"):
        walk(getattr(llm, name, None))
    return tuple(sorted(found)) or PROVIDER_NAMES


def _llm_defaults(llm, schema):
    defaults = {"backend": "openai_compatible", "backend_url": ""}
    builder = getattr(llm, "default_llm_config", None)
    if callable(builder):
        try:
            built = builder()
            if isinstance(built, dict):
                for key in ("backend", "backend_url"):
                    if isinstance(built.get(key), str):
                        defaults[key] = built[key]
        except Exception:
            pass
    return defaults


# ---------------------------------------------------------------------------
# Minimal TOML model that remembers the file's exact layout
# ---------------------------------------------------------------------------


class _Table(object):
    __slots__ = ("path", "start", "end", "keys")

    def __init__(self, path, start):
        self.path = path
        self.start = start          # index of the [header] line
        self.end = start + 1        # first line not in the table body
        self.keys = {}              # key -> (line_index, value_start, value_end)


_LINE_HEADER = re.compile(r"^\s*\[\[?\s*([A-Za-z0-9_.\-\"']+)\s*\]\]?\s*(#.*)?$")
_LINE_KEY = re.compile(
    r"^\s*([A-Za-z0-9_\-]+)\s*=\s*(\"(?:[^\"\\]|\\.)*\"|'[^']*'|[^\s#]+)"
)


def _parse_tables(text):
    # type: (str) -> list
    """Split ``text`` into tables, recording where each key's value starts."""
    tables = []
    current = _Table((), -1)
    in_multiline = None
    for index, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if in_multiline:
            if in_multiline in stripped:
                in_multiline = None
            continue
        if stripped.count('"""') % 2 or stripped.count("'''") % 2:
            in_multiline = '"""' if '"""' in stripped else "'''"
            continue
        header = _LINE_HEADER.match(line)
        if header and not stripped.startswith("#"):
            path = tuple(
                part.strip("\"'") for part in header.group(1).split(".")
            )
            current.end = index
            tables.append(current)
            current = _Table(path, index)
            continue
        assigned = _LINE_KEY.match(line)
        if assigned:
            value_start = line.index(assigned.group(2), assigned.start(1))
            current.keys[assigned.group(1)] = (
                index,
                value_start,
                value_start + len(assigned.group(2)),
            )
    current.end = len(text.splitlines())
    tables.append(current)
    return [table for table in tables if table.start >= -1 and table.keys]


def _toml_string(value):
    # type: (str) -> str
    return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')


def _realign(line, start, end, rendered):
    # type: (str, int, int, str) -> str
    """Replace ``line[start:end]`` with ``rendered``, padding so that a
    trailing comment keeps its original column."""
    rest = line[end:]
    if "#" in rest:
        gap = rest.index("#")
        padding = " " * max(1, end - start - len(rendered) + gap)
        return line[:start] + rendered + padding + rest[gap:]
    return line[:start] + rendered + rest


class Document(object):
    """Editable view over one TOML file's lines."""

    def __init__(self, text):
        # type: (str) -> None
        self.lines = text.splitlines(True)
        self.tables = _parse_tables(text)
        if not text.endswith("\n") and self.lines:
            self.lines[-1] += "\n"

    def text(self):
        # type: () -> str
        return "".join(self.lines)

    def table(self, path):
        # type: (tuple) -> object
        for table in self.tables:
            if table.path == path:
                return table
        return None

    def role_tables(self):
        # type: () -> dict
        return {
            table.path[2]: table
            for table in self.tables
            if len(table.path) == 3
            and table.path[:2] == ("llm", "role_llms")
        }

    def set_value(self, path, key, value):
        # type: (tuple, str, str) -> None
        rendered = _toml_string(value)
        table = self.table(path)
        if table is None:
            self._append_table(".".join(path), key, rendered)
            self._reparse()
            return
        if key in table.keys:
            index, start, end = table.keys[key]
            line = self.lines[index]
            # Keep an inline comment in the same column as before.
            self.lines[index] = _realign(line, start, end, rendered)
            return
        insert_at = table.start + 1
        for _, (index, _, _) in table.keys.items():
            insert_at = max(insert_at, index + 1)
        self.lines.insert(insert_at, "%s = %s\n" % (key, rendered))
        self._reparse(index=insert_at)

    def _append_table(self, dotted_path, key, rendered):
        # type: (str, str, str) -> None
        """Add a role table before any trailing comment block at end of file."""
        block = ["\n[%s]\n" % dotted_path, "%s = %s\n" % (key, rendered)]
        at = len(self.lines)
        while at > 0 and (
            not self.lines[at - 1].strip() or self.lines[at - 1].lstrip().startswith("#")
        ):
            at -= 1
        if at == len(self.lines):
            self.lines.extend(block)
        else:
            self.lines[at:at] = block

    def _reparse(self, index=None):
        # type: (object) -> None
        """Re-derive table positions after a structural edit (append/insert).

        In-place value edits keep every position, so only callers that change
        the line count pay for this.
        """
        text = self.text()
        self.tables = _parse_tables(text)
        self.lines = text.splitlines(True)

    def remove_value(self, path, key):
        # type: (tuple, str) -> None
        table = self.table(path)
        if table is not None and key in table.keys:
            del self.lines[table.keys[key][0]]

    def drop_role_table(self, role):
        # type: (str) -> None
        table = self.table(("llm", "role_llms", role))
        if table is not None:
            del self.lines[table.start:table.end]


def parse_document(text):
    # type: (str) -> dict
    if _tomllib is not None:
        return _tomllib.loads(text)
    raise SenateError(
        "SENATE_VALIDATION",
        "tomllib is unavailable; Python 3.11+ is required to read StockSenate config",
    )


# ---------------------------------------------------------------------------
# Payload validation
# ---------------------------------------------------------------------------


def _check_plain(value, label):
    # type: (object, str) -> str
    if not isinstance(value, str):
        raise SenateError("SENATE_INVALID_VALUE", "%s must be a string" % label)
    text = value.strip()
    if not text:
        raise SenateError("SENATE_INVALID_VALUE", "%s is required" % label)
    if len(text) > MAX_VALUE_LENGTH:
        raise SenateError("SENATE_INVALID_VALUE", "%s is too long" % label)
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in text):
        raise SenateError(
            "SENATE_INVALID_VALUE", "%s must not contain control characters" % label
        )
    return text


def _check_no_secret(key, value):
    # type: (str, object) -> None
    if not isinstance(key, str) or key.lower().endswith("_env"):
        return
    if _SECRET_KEY.search(key) and isinstance(value, str) and value.strip():
        raise SenateError(
            "SENATE_SECRET_REJECTED",
            "secrets must be stored in .env, not in llm_config.toml (key: %s)" % key,
            {"key": key},
        )
    if isinstance(value, str) and _USERINFO_URL.match(value.strip()):
        raise SenateError(
            "SENATE_SECRET_REJECTED",
            "URLs with embedded credentials are not allowed (key: %s)" % key,
            {"key": key},
        )


def _walk_secrets(node, path=()):
    # type: (object, tuple) -> None
    if isinstance(node, dict):
        for key, value in node.items():
            _check_no_secret(key, value)
            _walk_secrets(value, path + (key,))


def normalise_roles(rules, roles):
    # type: (SenateRules, object) -> tuple
    """Validate the UI's ``{role: {provider, model, api_key_env}}`` mapping.

    Returns ``(entries, warnings)`` where ``entries`` is a list of
    ``(canonical_role, provider, model, api_key_env)``.
    """
    if not isinstance(roles, dict) or not roles:
        raise SenateError("SENATE_INVALID_VALUE", "roles must be a non-empty object")
    if len(roles) > MAX_ROLES:
        raise SenateError("SENATE_INVALID_VALUE", "too many roles")
    entries = []
    warnings = []
    seen = {}
    for raw_role, spec in roles.items():
        canonical, warning = rules.canonical_role(raw_role)
        if warning:
            warnings.append(warning)
        if canonical in seen:
            raise SenateError(
                "SENATE_INVALID_ROLE",
                "role %s is given twice (%s and %s)"
                % (canonical, seen[canonical], raw_role),
                {"role": canonical},
            )
        seen[canonical] = raw_role
        if not isinstance(spec, dict):
            raise SenateError(
                "SENATE_INVALID_VALUE", "role %s must be an object" % raw_role
            )
        # Secrets first: a literal key must be reported as a secret, not as
        # merely an unsupported field.
        for key, value in spec.items():
            _check_no_secret(key, value)
        unknown = set(spec) - set(ROLE_KEYS_EDITED)
        if unknown:
            raise SenateError(
                "SENATE_INVALID_VALUE",
                "role %s has unsupported keys: %s"
                % (raw_role, ", ".join(sorted(map(str, unknown)))),
            )
        provider = _check_plain(spec.get("provider"), "%s.provider" % raw_role)
        if not _PROVIDER.match(provider.lower()):
            raise SenateError(
                "SENATE_INVALID_VALUE", "invalid provider name: %s" % provider
            )
        model = _check_plain(spec.get("model"), "%s.model" % raw_role)
        api_key_env = spec.get("api_key_env")
        if api_key_env is not None and str(api_key_env).strip():
            api_key_env = _check_plain(api_key_env, "%s.api_key_env" % raw_role)
            if not _ENV_NAME.match(api_key_env):
                raise SenateError(
                    "SENATE_INVALID_VALUE",
                    "%s.api_key_env must be an environment variable name"
                    % raw_role,
                )
        else:
            api_key_env = ""
        entries.append((canonical, provider.lower(), model, api_key_env))
    return entries, warnings


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def _audit(log_path, event):
    # type: (str, dict) -> None
    if not log_path:
        return
    import json

    line = json.dumps(event, ensure_ascii=False, sort_keys=True)
    try:
        directory = os.path.dirname(log_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        existing = []
        if os.path.isfile(log_path):
            with io.open(log_path, "r", encoding="utf-8") as handle:
                existing = handle.read().splitlines()
        existing = (existing + [line])[-MAX_AUDIT_LINES:]
        atomic_write_bytes(
            log_path, ("\n".join(existing) + "\n").encode("utf-8"), fsync=False
        )
    except (OSError, StoreError) as exc:
        LOGGER.warning("senate audit log not written: %s", exc)


# ---------------------------------------------------------------------------
# The public service object
# ---------------------------------------------------------------------------


class SenateRoleModels(object):
    """GET/PUT of role -> provider/model, backed by ``llm_config.toml``.

    One instance is shared by the bridge; a re-entrant lock serialises the
    read-modify-write so two overlapping saves cannot interleave.
    """

    def __init__(self, config_path=None, environ=None, audit_path=None):
        # type: (object, object, object) -> None
        self._explicit_path = config_path
        self._environ = environ
        self._audit_path = audit_path
        self._lock = threading.RLock()
        self._rules = None
        self._rules_dir = None

    # -- helpers ---------------------------------------------------------

    def _config_path(self):
        # type: () -> str
        return locate_config(self._environ, self._explicit_path)

    def _rules_for(self, config_path):
        # type: (str) -> SenateRules
        roles_dir = roles_dir_for(config_path)
        if self._rules is None or self._rules_dir != roles_dir:
            if not os.path.isdir(roles_dir):
                raise SenateError(
                    "SENATE_NOT_CONFIGURED",
                    "StockSenate checkout not found at %s" % roles_dir,
                    {"roles_dir": roles_dir,
                     "hint": "set STOCKSENATE_ROOT to the StockSenate repository"},
                )
            marker = os.path.join(roles_dir, "llm.py")
            if not os.path.isfile(marker):
                raise SenateError(
                    "SENATE_NOT_CONFIGURED",
                    "roles/llm.py not found: %s is not a StockSenate checkout"
                    % roles_dir,
                    {"roles_dir": roles_dir},
                )
            self._rules = load_rules(roles_dir)
            self._rules_dir = roles_dir
        return self._rules

    def _authority(self, llm, document, environ):
        # type: (object, dict, dict) -> str
        """Where role models are actually loaded from (mirrors loader.py)."""
        envoy = getattr(llm, "load_envoy_roles", None)
        if callable(envoy):
            try:
                from_env = envoy(environ)
            except Exception:
                from_env = None
            if from_env:
                return "env"
        return "toml"

    # -- GET -------------------------------------------------------------

    def get(self):
        # type: () -> dict
        with self._lock:
            config_path = self._config_path()
            rules = self._rules_for(config_path)
            environ = dict(self._environ if self._environ is not None else os.environ)
            raw = ""
            exists = os.path.isfile(config_path)
            if exists:
                with open(config_path, "rb") as handle:
                    payload = handle.read(MAX_FILE_BYTES + 1)
                if len(payload) > MAX_FILE_BYTES:
                    raise SenateError("SENATE_VALIDATION", "llm_config.toml is too large")
                raw = payload.decode("utf-8")
                document = parse_document(raw)
            else:
                document = {}
            llm_table = document.get("llm") if isinstance(document, dict) else None
            if not isinstance(llm_table, dict):
                llm_table = {}
            role_llms = llm_table.get("role_llms")
            if not isinstance(role_llms, dict):
                role_llms = {}

            configured = []
            unconfigured = []
            for role in rules.role_names:
                spec = role_llms.get(role)
                if isinstance(spec, dict) and spec.get("provider") and spec.get("model"):
                    configured.append(
                        {
                            "role": role,
                            "provider": str(spec.get("provider")),
                            "model": str(spec.get("model")),
                            "api_key_env": str(spec.get("api_key_env") or ""),
                        }
                    )
                else:
                    unconfigured.append(role)
            for role, spec in role_llms.items():
                if role not in rules.role_names:
                    unconfigured.append(str(role))
            unconfigured.sort()

            global_table = {}
            for key, value in llm_table.items():
                if key == "role_llms":
                    continue
                if _SECRET_KEY.search(str(key)) and not str(key).lower().endswith("_env"):
                    continue  # never echo a value that could be a key
                if isinstance(value, (str, int, float, bool)):
                    global_table[str(key)] = value

            providers = []
            for name in rules.providers:
                providers.append(
                    {
                        "name": name,
                        "key_env_suggested": SUGGESTED_KEY_ENV.get(name, ""),
                        "models": list(MODEL_SUGGESTIONS.get(name, ())),
                    }
                )

            return {
                "engine": {
                    "name": "StockSenate",
                    "label": "StockSenate 辩论引擎",
                    "config_path": config_path,
                    "roles_dir": rules.roles_dir,
                    "exists": exists,
                    "revision": _revision(exists, config_path),
                    "authority": self._authority(rules.llm, document, environ),
                    "reload": "webui_per_job",
                },
                "roles": configured,
                "unconfigured_roles": unconfigured,
                "role_count": len(rules.role_names),
                "global": global_table,
                "providers": providers,
                "aliases": dict(rules.alias_map),
            }

    # -- PUT -------------------------------------------------------------

    def put(self, roles, expected_revision=None):
        # type: (object, object) -> dict
        with self._lock:
            config_path = self._config_path()
            rules = self._rules_for(config_path)
            entries, warnings = normalise_roles(rules, roles)
            if not entries:
                raise SenateError("SENATE_INVALID_VALUE", "no roles to write")

            exists = os.path.isfile(config_path)
            if exists:
                current_revision = _revision(True, config_path)
                with open(config_path, "rb") as handle:
                    payload = handle.read(MAX_FILE_BYTES + 1)
                if len(payload) > MAX_FILE_BYTES:
                    raise SenateError("SENATE_VALIDATION", "llm_config.toml is too large")
                text = payload.decode("utf-8")
                if expected_revision and expected_revision != current_revision:
                    raise SenateError(
                        "SENATE_STALE",
                        "llm_config.toml changed since it was read; reload and retry",
                    )
            else:
                text = rules.defaults_toml()

            document = Document(text)
            roles_changed = 0
            for role, provider, model, api_key_env in entries:
                before = render_role(document, role)
                path = ("llm", "role_llms", role)
                document.set_value(path, "provider", provider)
                document.set_value(path, "model", model)
                if api_key_env:
                    document.set_value(path, "api_key_env", api_key_env)
                after = render_role(document, role)
                if before != after:
                    roles_changed += 1

            candidate_text = document.text()
            candidate = parse_document(candidate_text)
            _walk_secrets(candidate)
            llm, schema_warnings = rules.validate_document(candidate)
            written = _role_map(candidate)

            # Nothing drifted -> no write at all, but still validate.
            if exists and candidate_text == text:
                return {
                    "written": written,
                    "noop": True,
                    "warnings": warnings + schema_warnings,
                    "engine": self.get()["engine"],
                }

            backup_path = ""
            if exists:
                backup_path = config_path + BACKUP_SUFFIX
                try:
                    with open(config_path, "rb") as handle:
                        atomic_write_bytes(backup_path, handle.read(), fsync=False)
                except (OSError, StoreError) as exc:
                    raise SenateError(
                        "SENATE_WRITE_FAILED", "could not back up config: %s" % exc
                    )
            try:
                atomic_write_bytes(config_path, candidate_text.encode("utf-8"))
            except StoreError as exc:
                raise SenateError("SENATE_WRITE_FAILED", str(exc))

            # Re-read what is on disk and validate once more: the file, not the
            # string we built, is what StockSenate will load.
            with open(config_path, "rb") as handle:
                on_disk = parse_document(handle.read().decode("utf-8"))
            rules.validate_document(on_disk)

            revision = _revision(True, config_path)
            _audit(
                self._audit_path,
                {
                    "at": utc_now(),
                    "config_path": config_path,
                    "roles_changed": roles_changed,
                    "roles": [entry[0] for entry in entries],
                    "revision": revision,
                },
            )
            LOGGER.info(
                "senate role models written: %d role(s) -> %s (rev %s)",
                roles_changed,
                config_path,
                revision,
            )
            return {
                "written": _role_map(on_disk),
                "noop": False,
                "backup_path": backup_path,
                "warnings": warnings + schema_warnings,
                "engine": self.get()["engine"],
            }


def render_role(document, role):
    # type: (Document, str) -> tuple
    table = document.table(("llm", "role_llms", role))
    if table is None:
        return None
    values = []
    for key, (index, start, _end) in sorted(table.keys.items()):
        values.append((key, document.lines[index][start:].strip()))
    return tuple(values)


def _role_map(document):
    # type: (dict) -> dict
    llm = document.get("llm") if isinstance(document, dict) else None
    role_llms = (llm or {}).get("role_llms") if isinstance(llm, dict) else None
    if not isinstance(role_llms, dict):
        return {}
    return {
        str(role): {
            "provider": str(spec.get("provider", "")),
            "model": str(spec.get("model", "")),
            "api_key_env": str(spec.get("api_key_env") or ""),
        }
        for role, spec in role_llms.items()
        if isinstance(spec, dict)
    }


def _revision(exists, path):
    # type: (bool, str) -> str
    if not exists:
        return "absent"
    try:
        stat = os.stat(path)
    except OSError:
        return "absent"
    return "%d-%d" % (stat.st_mtime_ns, stat.st_size)
