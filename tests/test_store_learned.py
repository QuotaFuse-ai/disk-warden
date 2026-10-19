# -*- coding: utf-8 -*-
"""Learned rules: red is terminal, damaged files never turn rules green."""

import os
import tempfile
import unittest
from unittest import mock

from src.core.store import atomic
from src.core.store.learned import (
    LearnedRuleError,
    LearnedRulesStore,
    SAFETY_GREEN,
    SAFETY_RED,
)


def codes(store):
    return [warning["code"] for warning in store.warnings]


class LearnedRulesStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "config", "rules-learned.json")
        self.store = LearnedRulesStore(self.path)

    def _write(self, payload):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "wb") as handle:
            handle.write(atomic.dump_json_bytes(payload))

    def test_missing_file_is_empty_and_incomplete(self):
        self.assertEqual(self.store.load(), {})
        self.assertFalse(self.store.complete)
        self.assertIn("learned_missing", codes(self.store))

    def test_set_and_get_round_trip(self):
        self.assertTrue(self.store.mark_red("cursor:cache", reason="risky"))
        rule = self.store.get("cursor:cache")
        self.assertEqual(rule["safety"], SAFETY_RED)
        self.assertEqual(rule["reason"], "risky")
        self.assertEqual(rule["rule_id"], "learned")

    def test_red_is_terminal(self):
        self.assertTrue(self.store.mark_red("item"))
        self.assertFalse(self.store.mark_green("item"))
        self.assertEqual(self.store.get("item")["safety"], SAFETY_RED)

    def test_repeating_the_same_rule_does_not_rewrite(self):
        self.assertTrue(self.store.mark_green("item", reason="ok"))
        with mock.patch(
            "src.core.store.learned.atomic_write_json"
        ) as writer:
            self.assertFalse(self.store.mark_green("item", reason="ok"))
            writer.assert_not_called()

    def test_green_can_be_upgraded_to_red(self):
        self.assertTrue(self.store.mark_green("item"))
        self.assertTrue(self.store.mark_red("item", reason="rolled_back"))
        self.assertEqual(self.store.get("item")["safety"], SAFETY_RED)

    def test_remove(self):
        self.store.mark_red("item")
        self.assertTrue(self.store.remove("item"))
        self.assertFalse(self.store.remove("item"))
        self.assertEqual(self.store.load(), {})

    def test_invalid_safety_is_rejected(self):
        with self.assertRaises(LearnedRuleError):
            self.store.set_rule("item", "yellow")

    def test_empty_item_id_is_rejected(self):
        with self.assertRaises(LearnedRuleError):
            self.store.set_rule("  ", SAFETY_RED)

    def test_save_rejects_non_mapping(self):
        with self.assertRaises(LearnedRuleError):
            self.store.save([])

    def test_save_rejects_invalid_rule(self):
        with self.assertRaises(LearnedRuleError):
            self.store.save({"item": {"safety": "yellow"}})

    def test_invalid_entries_are_skipped_valid_ones_kept(self):
        self._write(
            {
                "rules": {
                    "good": {"safety": "red", "reason": "x", "rule_id": "r"},
                    "bad": {"safety": "amber"},
                    "string": "green",
                }
            }
        )
        rules = self.store.load()
        self.assertEqual(sorted(rules), ["good", "string"])
        self.assertEqual(rules["string"]["safety"], SAFETY_GREEN)
        self.assertFalse(self.store.complete)
        self.assertIn("learned_rule_invalid", codes(self.store))

    def test_bare_mapping_is_accepted(self):
        self._write({"item": "red"})
        rules = self.store.load()
        self.assertEqual(rules["item"]["safety"], SAFETY_RED)

    def test_non_mapping_payload_is_quarantined(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "wb") as handle:
            handle.write(atomic.dump_json_bytes(["nope"]))
        self.assertEqual(self.store.load(), {})
        self.assertIn("learned_corrupt", codes(self.store))

    def test_corrupt_file_is_quarantined_and_empty(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "wb") as handle:
            handle.write(b"{bad")
        self.assertEqual(self.store.load(), {})
        self.assertIn("learned_corrupt", codes(self.store))
        self.assertTrue(
            any(".corrupt-" in name for name in os.listdir(os.path.dirname(self.path)))
        )

    def test_empty_path_is_rejected(self):
        with self.assertRaises(atomic.StoreError):
            LearnedRulesStore("")


if __name__ == "__main__":
    unittest.main()
