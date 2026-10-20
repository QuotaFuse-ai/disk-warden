# -*- coding: utf-8 -*-
"""``AppService.rules_override_clear``: undo a manual mark."""

import unittest

from src.app.errors import BridgeError
from tests.app_fakes import make_item, make_service, set_snapshot

CURSOR_ROOT = r"C:\Users\alice\AppData\Roaming\Cursor"


def yellow_item(item_id="my-stuff", rel_path="MyStuff"):
    return make_item(
        item_id=item_id,
        source_path=CURSOR_ROOT + "\\" + rel_path,
        rel_path=rel_path,
        safety="yellow",
        source="unknown",
        rule_id="unknown",
        requires_confirm=True,
    )


class RulesOverrideClearTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service(self)

    def test_clear_restores_the_automatic_verdict(self):
        item = yellow_item()
        set_snapshot(self.service, [item])
        self.service.rules_override("my-stuff", "green", "looks safe")
        self.assertEqual(self.service._snapshot["items"][0]["safety"], "green")

        result = self.service.rules_override_clear("my-stuff")

        self.assertEqual(result, {"item_id": "my-stuff", "cleared": True})
        self.assertNotIn("my-stuff", self.service.learned.load())
        listed = self.service.rules_learned_list()["items"]
        self.assertEqual(listed, [])
        restored = self.service._snapshot["items"][0]
        self.assertEqual(restored["safety"], "yellow")
        self.assertEqual(restored["source"], "unknown")
        self.assertTrue(restored["requires_confirm"])

    def test_clear_leaves_other_manual_rules_alone(self):
        set_snapshot(
            self.service,
            [yellow_item(), yellow_item("other", "Other")],
        )
        self.service.rules_override("my-stuff", "green")
        self.service.rules_override("other", "green")

        self.service.rules_override_clear("my-stuff")

        rules = self.service.learned.load()
        self.assertEqual(sorted(rules), ["other"])
        self.assertEqual(rules["other"]["rule_id"], "manual")
        items = {item["item_id"]: item for item in self.service._snapshot["items"]}
        self.assertEqual(items["other"]["safety"], "green")
        self.assertEqual(items["my-stuff"]["safety"], "yellow")

    def test_missing_rule_is_not_found(self):
        with self.assertRaises(BridgeError) as caught:
            self.service.rules_override_clear("nope")
        self.assertEqual(caught.exception.code, "NOT_FOUND")
        self.assertEqual(caught.exception.detail["item_id"], "nope")

    def test_non_manual_rule_is_not_found_and_kept(self):
        self.service.learned.set_rule("auto-learned", "green", "pattern")
        with self.assertRaises(BridgeError) as caught:
            self.service.rules_override_clear("auto-learned")
        self.assertEqual(caught.exception.code, "NOT_FOUND")
        self.assertIn("auto-learned", self.service.learned.load())

    def test_empty_item_id_is_invalid(self):
        for value in ("", None, 7):
            with self.assertRaises(BridgeError) as caught:
                self.service.rules_override_clear(value)
            self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")

    def test_manual_mark_on_an_automatic_red_item_can_still_be_cleared(self):
        item = make_item(
            item_id="login-data",
            source_path=CURSOR_ROOT + "\\Login Data",
            rel_path="Login Data",
            safety="red",
        )
        set_snapshot(self.service, [item])
        self.service.rules_override("login-data", "red", "keep it")

        self.service.rules_override_clear("login-data")

        self.assertNotIn("login-data", self.service.learned.load())
        restored = self.service._snapshot["items"][0]
        self.assertEqual(restored["safety"], "red")
        self.assertEqual(restored["source"], "pattern")
        self.assertNotEqual(restored["rule_id"], "manual")


if __name__ == "__main__":
    unittest.main()
