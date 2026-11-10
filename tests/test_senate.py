# -*- coding: utf-8 -*-
"""GET/PUT contract for the StockSenate role-model bridge (QUOT-518).

A fake StockSenate checkout reproduces the parts of ``roles/llm.py`` and
``roles/schema.py`` the bridge depends on (role constants, alias table,
provider map, ``resolve_role_key``, ``validate_config``); the real modules are
exercised in ``test_real_checkout_if_present`` when ``STOCKSENATE_ROOT`` is set.
"""

import os
import shutil
import tempfile
import unittest

from src.app.bridge import handle_request
from src.core.ai import senate

ROLES_LLM = '''
_ROLE_ALIASES = {
    "technical": "market",
    "basic": "fundamentals",
    "sentiment": "social",
    "unlock": "lockup",
}
ROLE_MARKET = "market"
ROLE_FUNDAMENTALS = "fundamentals"
ROLE_NEWS = "news"
ROLE_SOCIAL = "social"
ROLE_BULL = "bull"
ROLE_BEAR = "bear"
ROLE_LOCKUP = "lockup"

_CLIENT_CACHE = {"openai": object(), "anthropic": object(), "google": object(),
                 "deepseek": object(), "openrouter": object(), "ollama": object()}
_PROVIDER_KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}

ROLE_NAMES = ("market", "fundamentals", "news", "social", "bull", "bear", "lockup")


def resolve_role_key(key):
    if key in ROLE_NAMES:
        return key
    return _ROLE_ALIASES.get(key)


def default_llm_config_toml():
    lines = ["# stock-senate default config", "[llm]",
             'backend = "openai_compatible"', 'backend_url = ""']
    for role in ROLE_NAMES:
        lines += ["", "[llm.role_llms.%s]" % role, 'provider = "openai"',
                  'model = "gpt-5"', 'api_key_env = "OPENAI_API_KEY"']
    return "\\n".join(lines) + "\\n"
'''

ROLES_SCHEMA = '''
class Result(object):
    def __init__(self, errors, warnings, config):
        self.errors = errors
        self.warnings = warnings
        self.config = config


class Llm(object):
    def __init__(self, data):
        self.data = data


class Config(object):
    def __init__(self, llm):
        self.llm = llm


def _check_llm(table, errors, warnings):
    roles = table.get("role_llms", {})
    aliases = {"technical": "market", "basic": "fundamentals",
               "sentiment": "social", "unlock": "lockup"}
    seen = set()
    for key, spec in roles.items():
        if key in aliases:
            warnings.append("alias %s -> %s" % (key, aliases[key]))
            if aliases[key] in roles:
                errors.append("role %s conflicts with its alias target" % key)
            key = aliases[key]
        elif key not in ("market", "fundamentals", "news", "social", "bull",
                         "bear", "lockup"):
            errors.append("unknown role key: %s" % key)
        if key in seen:
            errors.append("duplicate role: %s" % key)
        seen.add(key)
        if not spec.get("provider") or not spec.get("model"):
            errors.append("role %s needs provider and model" % key)


def validate_config(data):
    errors, warnings = [], []
    llm = data.get("llm")
    if not isinstance(llm, dict):
        return Result(["missing [llm]"], warnings, None)
    _check_llm(llm, errors, warnings)
    return Result(errors, warnings, Config(Llm(llm)))
'''

CONFIG = '''# StockSenate LLM configuration -- keep these comments!
# Managed by hand before QUOT-518.

[llm]
backend = "openai_compatible"   # openai-compatible gateway
backend_url = ""                # empty on purpose: overrides every provider if set
max_tokens = 2048

# --- debate roles -------------------------------------------------
[llm.role_llms.market]
provider = "openai"             # main workhorse
model = "gpt-5"
api_key_env = "OPENAI_API_KEY"

[llm.role_llms.fundamentals]
provider = "openai"
model = "gpt-5-mini"
api_key_env = "OPENAI_API_KEY"

[llm.role_llms.news]
provider = "anthropic"
model = "claude-sonnet-4-5"
api_key_env = "ANTHROPIC_API_KEY"

# trailing note that must survive
'''


def _full_comment_lines(text):
    return [
        line for line in text.splitlines()
        if line.lstrip().startswith("#")
    ]


class FakeCheckout(object):
    def __init__(self, root, config_text=CONFIG):
        self.root = root
        roles = os.path.join(root, "roles")
        os.makedirs(roles)
        with open(os.path.join(roles, "llm.py"), "w", encoding="utf-8") as handle:
            handle.write(ROLES_LLM)
        with open(os.path.join(roles, "schema.py"), "w", encoding="utf-8") as handle:
            handle.write(ROLES_SCHEMA)
        self.config_dir = os.path.join(root, "config")
        os.makedirs(self.config_dir)
        self.config_path = os.path.join(self.config_dir, "llm_config.toml")
        if config_text is not None:
            with open(self.config_path, "w", encoding="utf-8", newline="") as handle:
                handle.write(config_text)

    def read(self):
        with open(self.config_path, "rb") as handle:
            return handle.read().decode("utf-8")


class SenateTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dw-senate-")
        self.checkout = FakeCheckout(self.tmp)
        self.audit = os.path.join(self.tmp, "audit.log")
        self.gov = senate.SenateRoleModels(
            config_path=self.checkout.config_path,
            environ={},
            audit_path=self.audit,
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class GetTests(SenateTestCase):
    def test_get_lists_configured_roles_and_engine(self):
        view = self.gov.get()
        self.assertEqual(view["engine"]["name"], "StockSenate")
        self.assertEqual(view["engine"]["config_path"], self.checkout.config_path)
        self.assertTrue(view["engine"]["exists"])
        by_role = {item["role"]: item for item in view["roles"]}
        self.assertEqual(by_role["market"]["model"], "gpt-5")
        self.assertEqual(by_role["news"]["provider"], "anthropic")
        self.assertEqual(view["role_count"], 7)
        self.assertIn("lockup", view["unconfigured_roles"])
        self.assertEqual(view["global"]["backend"], "openai_compatible")
        self.assertNotIn("role_llms", view["global"])

    def test_get_never_echoes_secret_like_globals(self):
        with open(self.checkout.config_path, "a", encoding="utf-8") as handle:
            handle.write('api_key = "sk-live-should-not-leak"\n')
        text = repr(self.gov.get())
        self.assertNotIn("sk-live-should-not-leak", text)

    def test_get_catalog_marks_anthropic_key_env(self):
        view = self.gov.get()
        providers = {item["name"]: item for item in view["providers"]}
        self.assertEqual(
            providers["anthropic"]["key_env_suggested"], "ANTHROPIC_API_KEY"
        )
        self.assertTrue(providers["openai"]["models"])
        self.assertEqual(providers["ollama"]["key_env_suggested"], "")


class PutTests(SenateTestCase):
    def test_put_writes_and_preserves_comments_and_order(self):
        before = self.checkout.read()
        result = self.gov.put(
            {
                "market": {"provider": "anthropic", "model": "claude-opus-4-1",
                           "api_key_env": "ANTHROPIC_API_KEY"},
                "lockup": {"provider": "deepseek", "model": "deepseek-reasoner",
                           "api_key_env": "DEEPSEEK_API_KEY"},
            }
        )
        after = self.checkout.read()
        self.assertFalse(result["noop"])
        # Every original comment survives, in order, and untouched lines stay
        # byte-identical.
        comments = [line[line.index("#"):] for line in before.splitlines() if "#" in line]
        positions = [after.index(text) for text in comments]
        self.assertEqual(positions, sorted(positions))
        for line in _full_comment_lines(before):
            self.assertIn(line, after)
        self.assertLess(after.index("[llm.role_llms.market]"),
                        after.index("[llm.role_llms.fundamentals]"))
        self.assertIn('model = "claude-opus-4-1"', after)
        self.assertIn("[llm.role_llms.lockup]", after)
        # Inline comment stays in its original column.
        original_hash = [
            line for line in before.splitlines() if "# main workhorse" in line
        ][0].index("#")
        changed = [
            line for line in after.splitlines()
            if line.startswith("provider = ") and "# main workhorse" in line
        ][0]
        self.assertEqual(changed.index("#"), original_hash)
        # Untouched roles keep their values.
        self.assertIn('model = "gpt-5-mini"', after)
        # The new table lands before the file's trailing note.
        self.assertLess(after.index("[llm.role_llms.lockup]"),
                        after.index("# trailing note"))

    def test_put_passes_stock_senate_validation_on_disk(self):
        self.gov.put({"market": {"provider": "google", "model": "gemini-2.5-pro"}})
        rules = senate.load_rules(os.path.join(self.tmp, "roles"))
        with open(self.checkout.config_path, "rb") as handle:
            document = senate.parse_document(handle.read().decode("utf-8"))
        llm, _ = rules.validate_document(document)
        self.assertEqual(llm.data["role_llms"]["market"]["provider"], "google")

    def test_put_alias_is_canonicalised_with_warning(self):
        result = self.gov.put({"technical": {"provider": "openai", "model": "o4-mini"}})
        self.assertTrue(any(w.startswith("alias:technical->market") for w in result["warnings"]))
        self.assertIn("[llm.role_llms.market]", self.checkout.read())

    def test_put_unknown_role_rejected_before_write(self):
        before = self.checkout.read()
        with self.assertRaises(senate.SenateError) as caught:
            self.gov.put({"not_a_role": {"provider": "openai", "model": "gpt-5"}})
        self.assertEqual(caught.exception.code, "SENATE_INVALID_ROLE")
        self.assertEqual(self.checkout.read(), before)

    def test_put_missing_provider_or_model_rejected(self):
        with self.assertRaises(senate.SenateError) as caught:
            self.gov.put({"market": {"provider": "openai"}})
        self.assertEqual(caught.exception.code, "SENATE_INVALID_VALUE")

    def test_put_rejects_literal_secret(self):
        with self.assertRaises(senate.SenateError) as caught:
            self.gov.put(
                {"market": {"provider": "openai", "model": "gpt-5",
                            "api_key": "sk-live-123"}}
            )
        self.assertEqual(caught.exception.code, "SENATE_SECRET_REJECTED")
        self.assertNotIn("sk-live-123", self.checkout.read())

    def test_put_rejects_control_characters(self):
        with self.assertRaises(senate.SenateError):
            self.gov.put({"market": {"provider": "openai", "model": "gpt-5\n[evil]"}})

    def test_put_rejects_credential_in_url(self):
        with self.assertRaises(senate.SenateError) as caught:
            self.gov.put(
                {"market": {"provider": "openai", "model": "gpt-5",
                            "base_url": "https://user:pass@host/v1"}}
            )
        self.assertEqual(caught.exception.code, "SENATE_SECRET_REJECTED")

    def test_put_schema_error_does_not_touch_file(self):
        # An unknown role appended by hand must make the whole document fail
        # the way StockSenate's _check_llm would, so nothing is written.
        with open(self.checkout.config_path, "a", encoding="utf-8") as handle:
            handle.write('\n[llm.role_llms.bogus]\nprovider = "openai"\nmodel = "x"\n')
        before = self.checkout.read()
        with self.assertRaises(senate.SenateError) as caught:
            self.gov.put({"market": {"provider": "openai", "model": "gpt-4o"}})
        self.assertEqual(caught.exception.code, "SENATE_VALIDATION")
        self.assertEqual(self.checkout.read(), before)

    def test_put_is_noop_when_values_unchanged(self):
        first = self.gov.put({"market": {"provider": "openai", "model": "gpt-5",
                                         "api_key_env": "OPENAI_API_KEY"}})
        self.assertTrue(first["noop"])
        self.assertFalse(os.path.exists(self.checkout.config_path + ".bak"))

    def test_put_keeps_backup_and_writes_audit(self):
        self.gov.put({"market": {"provider": "google", "model": "gemini-2.5-pro"}})
        self.assertTrue(os.path.isfile(self.checkout.config_path + ".bak"))
        with open(self.audit, encoding="utf-8") as handle:
            audit = handle.read()
        self.assertIn('"roles_changed": 1', audit)

    def test_put_stale_revision_rejected(self):
        revision = self.gov.get()["engine"]["revision"]
        with open(self.checkout.config_path, "a", encoding="utf-8") as handle:
            handle.write("\n# edited elsewhere\n")
        with self.assertRaises(senate.SenateError) as caught:
            self.gov.put({"market": {"provider": "openai", "model": "gpt-4o"}},
                         expected_revision=revision)
        self.assertEqual(caught.exception.code, "SENATE_STALE")

    def test_put_creates_config_from_template(self):
        os.unlink(self.checkout.config_path)
        result = self.gov.put({"market": {"provider": "openai", "model": "gpt-5"}})
        self.assertFalse(result["noop"])
        text = self.checkout.read()
        self.assertIn("[llm]", text)
        self.assertIn("[llm.role_llms.market]", text)
        self.assertEqual(result["engine"]["exists"], True)
        self.assertIn("provider", result["written"]["market"])


class BridgeContractTests(SenateTestCase):
    def _rpc(self, method, params=None):
        import json

        from src.app.service import AppService

        service = AppService(
            data_root=os.path.join(self.tmp, "data"),
            senate_config_path=self.checkout.config_path,
        )
        try:
            response = handle_request(
                service,
                json.dumps({"v": 1, "id": "1", "method": method,
                            "params": params or {}}),
            )
        finally:
            service.close()
        return response

    def test_bridge_get_put_round_trip(self):
        got = self._rpc("senate.role_models.get")
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["result"]["role_count"], 7)
        put = self._rpc(
            "senate.role_models.put",
            {"roles": {"bear": {"provider": "deepseek",
                                "model": "deepseek-chat",
                                "api_key_env": "DEEPSEEK_API_KEY"}},
             "expected_revision": got["result"]["engine"]["revision"]},
        )
        self.assertTrue(put["ok"], put)
        self.assertEqual(put["result"]["written"]["bear"]["provider"], "deepseek")

    def test_bridge_error_envelope(self):
        failed = self._rpc(
            "senate.role_models.put", {"roles": {"nope": {"provider": "a", "model": "b"}}}
        )
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["error"]["code"], "SENATE_INVALID_ROLE")
        self.assertTrue(failed["error"]["message_key"].startswith("error."))


@unittest.skipUnless(os.environ.get("STOCKSENATE_ROOT"), "no StockSenate checkout")
class RealCheckoutTests(unittest.TestCase):
    def test_real_checkout_validates(self):
        root = os.environ["STOCKSENATE_ROOT"]
        rules = senate.load_rules(os.path.join(root, "roles"))
        self.assertIn("market", rules.role_names)
        self.assertEqual(rules.canonical_role("technical")[0], "market")
        self.assertIn("openai", rules.providers)


if __name__ == "__main__":
    unittest.main()
