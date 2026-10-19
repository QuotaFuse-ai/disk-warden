# -*- coding: utf-8 -*-
"""Plan registry and the red-path guard in ``plan.preview`` (architecture §5.5)."""

import math
import unittest

from src.app.errors import BridgeError
from src.app.plans import PlanRegistry
from src.app.service import SPACE_FACTOR, SPACE_MARGIN_BYTES
from tests.app_fakes import (
    FakeProcess,
    make_item,
    make_service,
    set_snapshot,
)


class PlanRegistryTest(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.registry = PlanRegistry(ttl_seconds=10, clock=lambda: self.now[0])

    def test_create_returns_identity_and_payload(self):
        plan = self.registry.create("migrate", {"items": ["a"], "total_bytes": 5})
        self.assertEqual(plan["kind"], "migrate")
        self.assertTrue(plan["plan_id"])
        self.assertTrue(plan["confirm_token"])
        self.assertTrue(plan["expires_at"].endswith("Z"))
        self.assertEqual(plan["items"], ["a"])
        self.assertEqual(plan["total_bytes"], 5)

    def test_peek_returns_a_copy(self):
        plan = self.registry.create("migrate", {"items": ["a"]})
        peeked = self.registry.peek(plan["plan_id"])
        peeked["items"].append("tampered")
        self.assertEqual(
            self.registry.peek(plan["plan_id"])["items"], ["a"]
        )

    def test_consume_is_one_shot(self):
        plan = self.registry.create("migrate", {"items": ["a"]})
        payload = self.registry.consume(plan["plan_id"], plan["confirm_token"])
        self.assertEqual(payload["items"], ["a"])
        with self.assertRaises(BridgeError) as ctx:
            self.registry.consume(plan["plan_id"], plan["confirm_token"])
        self.assertEqual(ctx.exception.code, "NOT_FOUND")

    def test_wrong_token_keeps_the_plan_alive(self):
        plan = self.registry.create("migrate", {"items": ["a"]})
        with self.assertRaises(BridgeError) as ctx:
            self.registry.consume(plan["plan_id"], "0" * 64)
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")
        self.assertIn("token", ctx.exception.detail["reason"])
        self.assertEqual(
            self.registry.consume(plan["plan_id"], plan["confirm_token"])["items"],
            ["a"],
        )

    def test_expired_plan_is_gone(self):
        plan = self.registry.create("migrate", {"items": ["a"]})
        self.now[0] += 11
        with self.assertRaises(BridgeError) as ctx:
            self.registry.consume(plan["plan_id"], plan["confirm_token"])
        self.assertEqual(ctx.exception.code, "NOT_FOUND")

    def test_tampered_payload_is_rejected(self):
        plan = self.registry.create(
            "migrate", {"items": [{"item_id": "a", "bytes": 1}]}
        )
        self.registry._plans[plan["plan_id"]]["payload"]["items"][0]["bytes"] = 999
        with self.assertRaises(BridgeError) as ctx:
            self.registry.consume(plan["plan_id"], plan["confirm_token"])
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")
        self.assertIn("content changed", ctx.exception.detail["reason"])

    def test_consume_requires_string_arguments(self):
        for plan_id, token in ((None, "t"), ("", "t"), ("p", None), ("p", ""), (1, "t")):
            with self.assertRaises(BridgeError) as ctx:
                self.registry.consume(plan_id, token)
            self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")

    def test_unknown_plan_is_not_found(self):
        with self.assertRaises(BridgeError) as ctx:
            self.registry.peek("missing")
        self.assertEqual(ctx.exception.code, "NOT_FOUND")

    def test_prune_reports_removed_entries(self):
        self.registry.create("migrate", {"items": ["a"]})
        self.registry.create("migrate", {"items": ["b"]})
        self.now[0] += 11
        self.assertEqual(self.registry.prune(), 2)
        self.assertEqual(self.registry.prune(), 0)

    def test_ttl_is_at_least_one_second(self):
        self.assertEqual(PlanRegistry(ttl_seconds=0).ttl_seconds, 1)


class PlanPreviewGuardTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service(self)

    def _preview(self, items, item_ids=None, confirmed=None):
        set_snapshot(self.service, items)
        ids = item_ids if item_ids is not None else [item["item_id"] for item in items]
        return self.service.plan_preview(ids, confirmed)

    def test_red_items_can_never_enter_a_plan(self):
        red = make_item(
            "cursor-login",
            safety="red",
            source_path=r"C:\Users\alice\AppData\Roaming\Cursor\Login Data",
            reason="登录凭证，禁止迁移",
            can_migrate=False,
            blocked_code="BLOCKED_RED_PATH",
        )
        green = make_item("cursor-cache")
        with self.assertRaises(BridgeError) as ctx:
            self._preview([green, red], confirmed=[red["item_id"]])
        error = ctx.exception
        self.assertEqual(error.code, "BLOCKED_RED_PATH")
        self.assertFalse(error.retryable)
        self.assertEqual(error.detail["items"][0]["item_id"], "cursor-login")
        self.assertEqual(error.detail["items"][0]["reason"], "登录凭证，禁止迁移")
        self.assertEqual(len(self.service.plans._plans), 0)

    def test_running_process_blocks_the_plan(self):
        service = make_service(
            self, process_provider=lambda: [FakeProcess(4321, "Cursor.exe")]
        )
        set_snapshot(service, [make_item("cursor-cache")])
        with self.assertRaises(BridgeError) as ctx:
            service.plan_preview(["cursor-cache"])
        error = ctx.exception
        self.assertEqual(error.code, "BLOCKED_PROCESS")
        self.assertTrue(error.retryable)
        self.assertEqual(error.detail["items"][0]["process"]["pids"], [4321])
        self.assertEqual(len(service.plans._plans), 0)

    def test_unreadable_directory_is_reported_with_an_error_count(self):
        broken = make_item("cursor-cache", access_errors=3)
        with self.assertRaises(BridgeError) as ctx:
            self._preview([broken])
        error = ctx.exception
        self.assertEqual(error.code, "PERMISSION_DENIED")
        self.assertTrue(error.retryable)
        self.assertEqual(error.detail["items"][0]["access_errors"], 3)

    def test_reparse_root_is_rejected(self):
        item = make_item("cursor-cache", is_reparse=True)
        with self.assertRaises(BridgeError) as ctx:
            self._preview([item])
        self.assertEqual(ctx.exception.code, "PERMISSION_DENIED")
        self.assertTrue(ctx.exception.detail["items"][0]["is_reparse"])

    def test_duplicate_final_paths_are_rejected(self):
        one = make_item("cursor-cache", source_path=r"C:\Users\alice\Roaming\Cache")
        two = make_item("cursor-cache-2", source_path=r"C:\Users\alice\Local\Cache")
        with self.assertRaises(BridgeError) as ctx:
            self._preview([one, two])
        error = ctx.exception
        self.assertEqual(error.code, "DUPLICATE_TARGET")
        self.assertEqual(len(error.detail["duplicates"]), 1)
        self.assertEqual(len(error.detail["duplicates"][0]["items"]), 2)

    def test_insufficient_space_is_rejected(self):
        service = make_service(self, free_space_provider=lambda path: 0)
        set_snapshot(service, [make_item("cursor-cache")])
        with self.assertRaises(BridgeError) as ctx:
            service.plan_preview(["cursor-cache"])
        error = ctx.exception
        self.assertEqual(error.code, "INSUFFICIENT_SPACE")
        self.assertTrue(error.retryable)
        self.assertEqual(error.detail["free_bytes"], 0)
        expected = (
            int(math.ceil(1024 * 1024 * SPACE_FACTOR)) + SPACE_MARGIN_BYTES
        )
        self.assertEqual(error.detail["required_bytes"], expected)

    def test_unknown_and_empty_item_lists_are_rejected(self):
        with self.assertRaises(BridgeError) as ctx:
            self.service.plan_preview([])
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")
        with self.assertRaises(BridgeError) as ctx:
            self._preview([make_item("cursor-cache")], item_ids=["nope"])
        self.assertEqual(ctx.exception.code, "NOT_FOUND")

    def test_preview_without_a_scan_is_rejected(self):
        with self.assertRaises(BridgeError) as ctx:
            self.service.plan_preview(["cursor-cache"])
        self.assertEqual(ctx.exception.code, "NOT_FOUND")

    def test_yellow_items_require_confirmation(self):
        yellow_one = make_item(
            "cursor-workspace",
            safety="yellow",
            source_path=r"C:\Users\alice\AppData\Roaming\Cursor\workspaceStorage",
        )
        yellow_two = make_item(
            "cursor-logs",
            safety="yellow",
            source_path=r"C:\Users\alice\AppData\Roaming\Cursor\logs",
        )
        plan = self._preview(
            [yellow_one, yellow_two], confirmed=["cursor-workspace"]
        )
        self.assertEqual(
            sorted(plan["yellow_item_ids"]),
            ["cursor-logs", "cursor-workspace"],
        )
        self.assertEqual(plan["unconfirmed_yellow"], ["cursor-logs"])
        self.assertTrue(plan["requires_confirm"])
        self.assertTrue(all(item["requires_confirm"] for item in plan["items"]))

        confirmed = self._preview(
            [yellow_one, yellow_two],
            confirmed=["cursor-workspace", "cursor-logs"],
        )
        self.assertEqual(confirmed["unconfirmed_yellow"], [])
        self.assertFalse(confirmed["requires_confirm"])

    def test_green_plan_carries_need_and_free_bytes(self):
        first = make_item("cursor-cache")
        second = make_item(
            "cursor-logs",
            source_path=r"C:\Users\alice\AppData\Roaming\Cursor\logs",
        )
        plan = self._preview([first, second])
        total = first["bytes"] + second["bytes"]
        self.assertEqual(plan["total_bytes"], total)
        self.assertEqual(
            plan["need_bytes"],
            int(math.ceil(total * SPACE_FACTOR)) + SPACE_MARGIN_BYTES,
        )
        self.assertEqual(plan["free_bytes"], 4 * 1024 ** 3)
        self.assertFalse(plan["requires_confirm"])
        self.assertIn("confirm_token", plan)
        self.assertIn("expires_at", plan)


if __name__ == "__main__":
    unittest.main()
