# -*- coding: utf-8 -*-
"""Unit tests for the rules layer (T07).

Coverage:
  * documented rule table from ``docs/目录清单.md`` §3
  * case / space / separator handling
  * green / red / yellow verdicts and conflict priority (red_wins)
  * catalog > learned > name rules > unknown
  * learned constraints (red is terminal, yellow -> green)
  * AI interface (yellow only, always needs confirmation)
  * strict data loading and unresolved environment variables
"""

import json
import unittest
from pathlib import Path

from src.core.rules import (
    AISuggestion,
    ClassifyEngine,
    LearnedRule,
    RuleItem,
    RulesError,
    SAFETY_GREEN,
    SAFETY_RED,
    SAFETY_YELLOW,
    SOURCE_AI,
    SOURCE_CATALOG,
    SOURCE_LEARNED,
    SOURCE_PATTERN,
    SOURCE_UNKNOWN,
    item_id_for,
    load_engine,
    parse_catalog,
    parse_name_rules,
)

ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = ROOT / "data" / "rules.json"
CATALOG_PATH = ROOT / "data" / "ide-catalog.json"

ENV = {
    "APPDATA": r"C:\Users\tester\AppData\Roaming",
    "LOCALAPPDATA": r"C:\Users\tester\AppData\Local",
    "USERPROFILE": r"C:\Users\tester",
}

GREEN_EXACT = {
    "Cache",
    "CachedData",
    "GPUCache",
    "Code Cache",
    "logs",
    "debug",
    "sessions",
    "projects",
    "tool-results",
    "checkpoints",
    "workspaceStorage",
    "globalStorage",
    "image-cache",
    "paste-cache",
    "uploads",
}

RED_EXACT = {
    "Login Data",
    "Local State",
    "settings.json",
    "settings.local.json",
    ".credentials.json",
    ".credentials.yaml",
    "auth.json",
    ".env",
    "memory",
    "memories",
}


def rule(match, safety, match_type="exact", reason=None):
    return {
        "match": match,
        "match_type": match_type,
        "safety": safety,
        "reason": reason or "Documented reason for %s" % match,
    }


def rules_doc(rules, default=None):
    return {
        "schema_version": 1,
        "name_scope": "basename",
        "case_sensitive": False,
        "conflict_resolution": "red_wins",
        "default": default
        or {"safety": "yellow", "reason": "Unknown directory, user must decide"},
        "rules": rules,
    }


def neutral_rules():
    """Rules document with no rule that can match a realistic name."""
    return rules_doc([rule("__rules_layer_no_match__", "green")])


def catalog_entry(path, safety="green", entry_type="dir", reason=None):
    return {
        "path": path,
        "type": entry_type,
        "safety": safety,
        "reason": reason or "Catalog reason for %s" % path,
    }


def catalog_doc(entries, tool_id="demo"):
    return {
        "schema_version": 1,
        "source": "tests",
        "platform": "windows",
        "updated_at": "2026-10-19",
        "ides": [
            {
                "id": tool_id,
                "name": "Demo",
                "kind": "ide",
                "platform": "windows",
                "processes": ["demo.exe"],
                "directories": entries,
            }
        ],
    }


class FakeResolver(object):
    def __init__(self, safety):
        self.safety = safety
        self.calls = []

    def classify(self, item, current):
        self.calls.append((item, current))
        if self.safety is None:
            return None
        return AISuggestion(self.safety)


class RulesLayerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.real_engine = load_engine(
            str(RULES_PATH), str(CATALOG_PATH), environ=ENV
        )

    def make_engine(self, rules=None, catalog=None, learned=None, ai_resolver=None):
        name_rules = parse_name_rules(rules or neutral_rules())
        index = parse_catalog(catalog, environ=ENV) if catalog else None
        return ClassifyEngine(
            name_rules,
            catalog_index=index,
            learned=learned,
            ai_resolver=ai_resolver,
        )

    # ------------------------------------------------------------------
    # 1. rule table / hit rate
    # ------------------------------------------------------------------
    def test_rule_file_matches_documented_table(self):
        exact_green = set()
        exact_red = set()
        globs = set()
        for compiled in self.real_engine.name_rules.rules:
            if compiled.safety == SAFETY_GREEN:
                if compiled.match_type == "exact":
                    exact_green.add(compiled.match)
                else:
                    globs.add(compiled.match)
            elif compiled.safety == SAFETY_RED:
                exact_red.add(compiled.match)

        self.assertEqual(exact_green, GREEN_EXACT)
        self.assertEqual(exact_red, RED_EXACT)
        self.assertEqual(globs, {"*.log", "*.sqlite"})
        self.assertEqual(len(self.real_engine.name_rules), 27)

    def test_documented_green_names_are_green(self):
        for name in GREEN_EXACT:
            verdict = self.real_engine.classify_name(name)
            self.assertEqual(verdict.safety, SAFETY_GREEN, name)
            self.assertEqual(verdict.source, SOURCE_PATTERN, name)

    def test_documented_red_names_are_red(self):
        for name in RED_EXACT:
            verdict = self.real_engine.classify_name(name)
            self.assertEqual(verdict.safety, SAFETY_RED, name)
            self.assertEqual(verdict.source, SOURCE_PATTERN, name)

    def test_documented_globs_are_green(self):
        for name in ("app.log", "DEBUG.LOG", "state.sqlite", "state.SQLITE"):
            self.assertEqual(
                self.real_engine.classify_name(name).safety, SAFETY_GREEN, name
            )
        self.assertEqual(
            self.real_engine.classify_name("notes.txt").safety, SAFETY_YELLOW
        )

    def test_unknown_name_is_yellow_unknown(self):
        verdict = self.real_engine.classify_name("totally-new-directory")
        self.assertEqual(verdict.safety, SAFETY_YELLOW)
        self.assertEqual(verdict.source, SOURCE_UNKNOWN)

    # ------------------------------------------------------------------
    # 2. case / spaces / separators
    # ------------------------------------------------------------------
    def test_case_insensitive(self):
        for name in ("cache", "CACHE", "CaChE", "Login Data", "LOGIN DATA", ".ENV"):
            verdict = self.real_engine.classify_name(name)
            self.assertIn(verdict.safety, (SAFETY_GREEN, SAFETY_RED), name)

    def test_internal_spaces_are_significant(self):
        self.assertEqual(
            self.real_engine.classify_name("Code Cache").safety, SAFETY_GREEN
        )
        self.assertEqual(
            self.real_engine.classify_name("CodeCache").safety, SAFETY_YELLOW
        )
        self.assertEqual(
            self.real_engine.classify_name("Login Data").safety, SAFETY_RED
        )
        self.assertEqual(
            self.real_engine.classify_name("LoginData").safety, SAFETY_YELLOW
        )

    def test_windows_trailing_spaces_and_dots_are_ignored(self):
        for name in ("Cache ", "Cache.", "Cache . ", "Login Data."):
            verdict = self.real_engine.classify_name(name)
            self.assertIn(verdict.safety, (SAFETY_GREEN, SAFETY_RED), repr(name))

    def test_both_separators_and_trailing_separator(self):
        for path in (
            r"some\where\Cache",
            "some/where/Cache",
            "some/where/Cache/",
            r"some\where\Login Data",
            "some/where/Login Data/",
        ):
            verdict = self.real_engine.classify_name(path)
            self.assertIn(verdict.safety, (SAFETY_GREEN, SAFETY_RED), path)

    # ------------------------------------------------------------------
    # 3. conflict priority
    # ------------------------------------------------------------------
    def test_red_wins_over_green_for_same_name(self):
        engine = self.make_engine(
            rules_doc(
                [
                    rule("*", "green", match_type="glob"),
                    rule("shared", "red"),
                ]
            )
        )
        verdict = engine.classify_name("shared")
        self.assertEqual(verdict.safety, SAFETY_RED)

    def test_red_glob_wins_over_green_exact(self):
        engine = self.make_engine(
            rules_doc(
                [
                    rule("settings.json", "green"),
                    rule("*.json", "red", match_type="glob"),
                ]
            )
        )
        self.assertEqual(engine.classify_name("settings.json").safety, SAFETY_RED)

    def test_green_wins_when_no_red_matches(self):
        engine = self.make_engine(
            rules_doc(
                [
                    rule("*", "green", match_type="glob"),
                    rule("odd", "yellow"),
                ]
            )
        )
        self.assertEqual(engine.classify_name("anything").safety, SAFETY_GREEN)
        self.assertEqual(engine.classify_name("odd").safety, SAFETY_GREEN)

    def test_yellow_rule_is_used_when_alone(self):
        engine = self.make_engine(rules_doc([rule("maybe", "yellow")]))
        verdict = engine.classify_name("maybe")
        self.assertEqual(verdict.safety, SAFETY_YELLOW)
        self.assertEqual(verdict.source, SOURCE_PATTERN)

    # ------------------------------------------------------------------
    # 4. yellow / red migration safety
    # ------------------------------------------------------------------
    def test_yellow_unknown_is_not_migratable(self):
        path = r"C:\Users\tester\AppData\Roaming\Demo\Mystery"
        classification = self.real_engine.classify(RuleItem("demo", path, "dir"))
        self.assertEqual(classification.safety, SAFETY_YELLOW)
        self.assertTrue(classification.requires_confirm)
        self.assertFalse(classification.can_migrate)

    def test_red_never_migratable_or_confirmable(self):
        paths = [
            r"C:\Users\tester\AppData\Roaming\Cursor\Login Data",
            r"C:\Users\tester\AppData\Roaming\Cursor\User\settings.json",
            r"C:\Users\tester\.claude\projects\demo\memory",
        ]
        for path in paths:
            classification = self.real_engine.classify(RuleItem("cursor", path, "dir"))
            self.assertEqual(classification.safety, SAFETY_RED, path)
            self.assertFalse(classification.requires_confirm, path)
            self.assertFalse(classification.can_migrate, path)

    def test_green_is_migratable(self):
        path = r"C:\Users\tester\AppData\Roaming\Cursor\Cache"
        classification = self.real_engine.classify(RuleItem("cursor", path, "dir"))
        self.assertEqual(classification.safety, SAFETY_GREEN)
        self.assertEqual(classification.source, SOURCE_CATALOG)
        self.assertFalse(classification.requires_confirm)
        self.assertTrue(classification.can_migrate)

    # ------------------------------------------------------------------
    # 5. catalog matching and priority
    # ------------------------------------------------------------------
    def test_catalog_takes_priority_over_name_rules(self):
        engine = self.make_engine(
            rules=rules_doc([rule("Cache", "green")]),
            catalog=catalog_doc(
                [catalog_entry("%APPDATA%\\Demo\\Cache\\", safety="yellow")]
            ),
        )
        classification = engine.classify(
            RuleItem("demo", r"C:\Users\tester\AppData\Roaming\Demo\Cache", "dir")
        )
        self.assertEqual(classification.safety, SAFETY_YELLOW)
        self.assertEqual(classification.source, SOURCE_CATALOG)
        self.assertTrue(classification.requires_confirm)

    def test_catalog_can_mark_unknown_name_green(self):
        engine = self.make_engine(
            rules=neutral_rules(),
            catalog=catalog_doc(
                [catalog_entry("%APPDATA%\\Demo\\StrangeName\\")]
            ),
        )
        classification = engine.classify(
            RuleItem("demo", r"C:\Users\tester\AppData\Roaming\Demo\StrangeName", "dir")
        )
        self.assertEqual(classification.safety, SAFETY_GREEN)
        self.assertEqual(classification.source, SOURCE_CATALOG)

    def test_more_specific_catalog_entry_wins(self):
        engine = self.make_engine(
            rules=neutral_rules(),
            catalog=catalog_doc(
                [
                    catalog_entry("%APPDATA%\\Demo\\", safety="yellow"),
                    catalog_entry("%APPDATA%\\Demo\\Projects\\", safety="green"),
                ]
            ),
        )
        parent = engine.classify(
            RuleItem("demo", r"C:\Users\tester\AppData\Roaming\Demo", "dir")
        )
        child = engine.classify(
            RuleItem(
                "demo", r"C:\Users\tester\AppData\Roaming\Demo\Projects", "dir"
            )
        )
        self.assertEqual(parent.safety, SAFETY_YELLOW)
        self.assertEqual(child.safety, SAFETY_GREEN)

    def test_catalog_glob_entry(self):
        engine = self.make_engine(
            rules=neutral_rules(),
            catalog=catalog_doc(
                [
                    catalog_entry(
                        r"%APPDATA%\Demo\workspaceStorage\*.pack",
                        entry_type="glob",
                    )
                ]
            ),
        )
        hit = engine.classify(
            RuleItem(
                "demo",
                r"C:\Users\tester\AppData\Roaming\Demo\workspaceStorage\one.pack",
                "file",
            )
        )
        miss = engine.classify(
            RuleItem(
                "demo",
                r"C:\Users\tester\AppData\Roaming\Demo\workspaceStorage\one.txt",
                "file",
            )
        )
        self.assertEqual(hit.safety, SAFETY_GREEN)
        self.assertEqual(hit.source, SOURCE_CATALOG)
        self.assertEqual(miss.safety, SAFETY_YELLOW)

    def test_catalog_kind_must_match(self):
        engine = self.make_engine(
            rules=neutral_rules(),
            catalog=catalog_doc(
                [catalog_entry(r"%APPDATA%\Demo\state.vscdb", entry_type="file")]
            ),
        )
        path = r"C:\Users\tester\AppData\Roaming\Demo\state.vscdb"
        self.assertEqual(engine.classify(RuleItem("demo", path, "file")).safety, SAFETY_GREEN)
        self.assertEqual(engine.classify(RuleItem("demo", path, "dir")).safety, SAFETY_YELLOW)

    def test_catalog_placeholders_and_case(self):
        engine = self.make_engine(
            rules=neutral_rules(),
            catalog=catalog_doc(
                [catalog_entry("%USERPROFILE%\\.demo\\projects\\<项目>\\")]
            ),
        )
        path = r"c:/users/TESTER/.demo/projects/my-app"
        classification = engine.classify(RuleItem("demo", path, "dir"))
        self.assertEqual(classification.safety, SAFETY_GREEN)
        self.assertEqual(classification.source, SOURCE_CATALOG)

    def test_unresolved_environment_is_a_warning_not_a_crash(self):
        data = catalog_doc(
            [
                catalog_entry("%APPDATA%\\Demo\\Cache\\"),
                catalog_entry("%USERPROFILE%\\.demo\\"),
            ]
        )
        index = parse_catalog(data, environ={"USERPROFILE": ENV["USERPROFILE"]})
        self.assertEqual(len(index), 1)
        self.assertEqual(len(index.warnings), 1)
        self.assertEqual(index.warnings[0].code, "unresolved_environment")

    def test_environment_lookup_is_case_insensitive(self):
        data = catalog_doc([catalog_entry("%appdata%\\Demo\\Cache\\")])
        index = parse_catalog(data, environ={"appdata": ENV["APPDATA"]})
        self.assertEqual(len(index), 1)

    def test_claude_code_parent_and_child_safety(self):
        projects = self.real_engine.classify(
            RuleItem(
                "claude-code",
                r"C:\Users\tester\.claude\projects\demo",
                "dir",
            )
        )
        tool_results = self.real_engine.classify(
            RuleItem(
                "claude-code",
                r"C:\Users\tester\.claude\projects\demo\s1\tool-results",
                "dir",
            )
        )
        memory = self.real_engine.classify(
            RuleItem(
                "claude-code",
                r"C:\Users\tester\.claude\projects\demo\memory",
                "dir",
            )
        )
        self.assertEqual(projects.safety, SAFETY_GREEN)
        self.assertEqual(tool_results.safety, SAFETY_GREEN)
        self.assertEqual(memory.safety, SAFETY_RED)
        self.assertFalse(memory.can_migrate)

    def test_shared_global_storage_settings_are_red(self):
        classification = self.real_engine.classify(
            RuleItem(
                "vscode",
                r"C:\Users\tester\AppData\Roaming\Code\User\globalStorage\settings.json",
                "file",
            )
        )
        self.assertEqual(classification.safety, SAFETY_RED)
        self.assertFalse(classification.can_migrate)

    # ------------------------------------------------------------------
    # 6. learned rules
    # ------------------------------------------------------------------
    def test_learned_can_resolve_yellow_to_green(self):
        path = r"C:\Users\tester\AppData\Roaming\Demo\Mystery"
        item_id = item_id_for("demo", source_path=path)
        engine = self.make_engine(
            rules=neutral_rules(), learned={item_id: "green"}
        )
        classification = engine.classify(RuleItem("demo", path, "dir"))
        self.assertEqual(classification.safety, SAFETY_GREEN)
        self.assertEqual(classification.source, SOURCE_LEARNED)
        self.assertTrue(classification.can_migrate)

    def test_learned_can_escalate_green_to_red(self):
        path = r"C:\Users\tester\AppData\Roaming\Demo\Cache"
        item_id = item_id_for("demo", source_path=path)
        engine = self.make_engine(
            rules=rules_doc([rule("Cache", "green")]),
            learned=[LearnedRule(item_id=item_id, safety="red")],
        )
        classification = engine.classify(RuleItem("demo", path, "dir"))
        self.assertEqual(classification.safety, SAFETY_RED)
        self.assertEqual(classification.source, SOURCE_LEARNED)
        self.assertFalse(classification.can_migrate)

    def test_learned_cannot_soften_red(self):
        path = r"C:\Users\tester\AppData\Roaming\Demo\Login Data"
        item_id = item_id_for("demo", source_path=path)
        engine = self.make_engine(
            rules=rules_doc([rule("Login Data", "red")]),
            learned={item_id: "green"},
        )
        classification = engine.classify(RuleItem("demo", path, "file"))
        self.assertEqual(classification.safety, SAFETY_RED)
        self.assertEqual(classification.source, SOURCE_PATTERN)

    def test_learned_green_keeps_existing_green_provenance(self):
        path = r"C:\Users\tester\AppData\Roaming\Demo\Cache"
        item_id = item_id_for("demo", source_path=path)
        engine = self.make_engine(
            rules=rules_doc([rule("Cache", "green")]),
            learned={item_id: "green"},
        )
        classification = engine.classify(RuleItem("demo", path, "dir"))
        self.assertEqual(classification.safety, SAFETY_GREEN)
        self.assertEqual(classification.source, SOURCE_PATTERN)

    def test_learned_yellow_is_rejected(self):
        with self.assertRaises(RulesError):
            self.make_engine(rules=neutral_rules(), learned={"abc": "yellow"})

    # ------------------------------------------------------------------
    # 7. AI interface (offline, yellow only)
    # ------------------------------------------------------------------
    def test_ai_is_not_called_for_red_or_green(self):
        resolver = FakeResolver(SAFETY_GREEN)
        engine = self.make_engine(
            rules=rules_doc([rule("Cache", "green"), rule("Login Data", "red")]),
            ai_resolver=resolver,
        )
        engine.classify(
            RuleItem("demo", r"C:\Users\tester\AppData\Roaming\Demo\Cache", "dir")
        )
        engine.classify(
            RuleItem("demo", r"C:\Users\tester\AppData\Roaming\Demo\Login Data", "file")
        )
        self.assertEqual(resolver.calls, [])

    def test_ai_can_resolve_yellow_but_requires_confirmation(self):
        resolver = FakeResolver(SAFETY_GREEN)
        engine = self.make_engine(rules=neutral_rules(), ai_resolver=resolver)
        classification = engine.classify(
            RuleItem("demo", r"C:\Users\tester\AppData\Roaming\Demo\Mystery", "dir")
        )
        self.assertEqual(classification.safety, SAFETY_GREEN)
        self.assertEqual(classification.source, SOURCE_AI)
        self.assertTrue(classification.requires_confirm)
        self.assertFalse(classification.can_migrate)
        self.assertEqual(len(resolver.calls), 1)

    def test_ai_none_keeps_yellow(self):
        engine = self.make_engine(rules=neutral_rules(), ai_resolver=FakeResolver(None))
        classification = engine.classify(
            RuleItem("demo", r"C:\Users\tester\AppData\Roaming\Demo\Mystery", "dir")
        )
        self.assertEqual(classification.safety, SAFETY_YELLOW)
        self.assertEqual(classification.source, SOURCE_UNKNOWN)

    # ------------------------------------------------------------------
    # 8. item id and bulk classification
    # ------------------------------------------------------------------
    def test_item_id_is_stable_across_path_spelling(self):
        first = item_id_for("cursor", source_path=r"C:\Users\A\AppData\Roaming\Cursor\Cache")
        second = item_id_for("cursor", source_path="c:/users/a/AppData/Roaming/Cursor/Cache/")
        self.assertEqual(first, second)
        self.assertEqual(len(first), 16)

    def test_explicit_item_id_is_preserved(self):
        classification = self.real_engine.classify(
            RuleItem(
                "cursor",
                r"C:\Users\tester\AppData\Roaming\Cursor\Cache",
                "dir",
                item_id="fixed-id",
            )
        )
        self.assertEqual(classification.item_id, "fixed-id")

    def test_classify_many_keeps_order(self):
        items = [
            RuleItem("cursor", r"C:\Users\tester\AppData\Roaming\Cursor\Cache", "dir"),
            RuleItem("cursor", r"C:\Users\tester\AppData\Roaming\Cursor\Login Data", "file"),
        ]
        results = self.real_engine.classify_many(items)
        self.assertEqual([item.safety for item in results], [SAFETY_GREEN, SAFETY_RED])

    # ------------------------------------------------------------------
    # 9. strict loading
    # ------------------------------------------------------------------
    def test_loader_rejects_invalid_rule_documents(self):
        cases = []
        doc = rules_doc([rule("Cache", "green")])
        doc["schema_version"] = 2
        cases.append(("schema version", doc))

        doc = rules_doc([rule("Cache", "green")])
        doc["name_scope"] = "path"
        cases.append(("name scope", doc))

        doc = rules_doc([rule("Cache", "green")])
        doc["case_sensitive"] = True
        cases.append(("case sensitivity", doc))

        doc = rules_doc([rule("Cache", "green")])
        doc["conflict_resolution"] = "green_wins"
        cases.append(("conflict resolution", doc))

        cases.append(("empty rules", rules_doc([])))
        cases.append(("path in match", rules_doc([rule(r"foo\bar", "green")])))
        cases.append(("leading space", rules_doc([rule(" Cache", "green")])))
        cases.append(("exact wildcard", rules_doc([rule("Ca*", "green")])))
        cases.append(
            ("glob without wildcard", rules_doc([rule("cache", "green", "glob")]))
        )
        cases.append(("bad regex", rules_doc([rule("[", "green", "regex")])))
        cases.append(("bad match type", rules_doc([rule("Cache", "green", "prefix")])))
        cases.append(("bad safety", rules_doc([rule("Cache", "blue")])))
        cases.append(("short reason", rules_doc([rule("Cache", "green", reason="x")])))
        cases.append(
            (
                "duplicate rule",
                rules_doc([rule("Cache", "green"), rule("cache", "red")]),
            )
        )
        cases.append(
            (
                "default not yellow",
                rules_doc(
                    [rule("Cache", "green")],
                    default={"safety": "green", "reason": "default green"},
                ),
            )
        )

        for label, document in cases:
            with self.assertRaises(RulesError, msg=label):
                parse_name_rules(document)

    def test_catalog_loader_rejects_invalid_documents(self):
        bad_docs = [
            [],
            {"ides": "nope"},
            catalog_doc([{"path": "%APPDATA%\\Demo\\", "type": "dir", "reason": "r"}]),
            catalog_doc(
                [{"path": "%APPDATA%\\Demo\\", "type": "dir", "safety": "blue", "reason": "r"}]
            ),
            catalog_doc([{"path": "%APPDATA%\\Demo\\", "type": "dir", "safety": "green"}]),
        ]
        for document in bad_docs:
            with self.assertRaises(RulesError):
                parse_catalog(document, environ=ENV)


if __name__ == "__main__":
    unittest.main()
