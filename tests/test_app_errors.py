# -*- coding: utf-8 -*-
"""Error envelope mapping for the bridge (architecture §5.4)."""

import unittest

from src.app.errors import ERROR_CODES, BridgeError, invalid, not_found


class ErrorCodeMapTest(unittest.TestCase):
    def test_message_key_is_derived_from_code(self):
        for code in ERROR_CODES:
            error = BridgeError(code)
            self.assertEqual(error.code, code)
            self.assertEqual(error.message_key, "error." + code)

    def test_retryable_defaults_match_the_contract(self):
        # A migrated-or-not decision must never be silently retried, while
        # transient engine failures may be.
        self.assertTrue(BridgeError("BUSY").retryable)
        self.assertTrue(BridgeError("BLOCKED_PROCESS").retryable)
        self.assertTrue(BridgeError("INSUFFICIENT_SPACE").retryable)
        self.assertTrue(BridgeError("PERMISSION_DENIED").retryable)
        self.assertTrue(BridgeError("VERIFY_FAILED").retryable)
        self.assertFalse(BridgeError("BLOCKED_RED_PATH").retryable)
        self.assertFalse(BridgeError("AI_CONSENT_REQUIRED").retryable)
        self.assertFalse(BridgeError("INVALID_ARGUMENT").retryable)
        self.assertFalse(BridgeError("INTERNAL").retryable)

    def test_unknown_code_is_coerced_to_internal(self):
        error = BridgeError("NOT_A_REAL_CODE", "boom")
        self.assertEqual(error.code, "INTERNAL")
        self.assertEqual(error.message_key, "error.INTERNAL")
        self.assertFalse(error.retryable)

    def test_retryable_can_be_overridden(self):
        self.assertFalse(BridgeError("IO_ERROR", retryable=False).retryable)
        self.assertTrue(BridgeError("BLOCKED_RED_PATH", retryable=True).retryable)

    def test_detail_is_copied_and_reason_is_added(self):
        detail = {"item_id": "x"}
        error = BridgeError("NOT_FOUND", "gone", detail)
        self.assertIsNot(error.detail, detail)
        self.assertEqual(error.detail["item_id"], "x")
        self.assertEqual(error.detail["reason"], "gone")
        self.assertNotIn("reason", detail)

    def test_detail_keeps_an_explicit_reason(self):
        error = BridgeError("NOT_FOUND", "outer", {"reason": "inner"})
        self.assertEqual(error.detail["reason"], "inner")

    def test_non_mapping_detail_becomes_empty(self):
        self.assertEqual(BridgeError("INTERNAL", "x", ["nope"]).detail, {"reason": "x"})
        self.assertEqual(BridgeError("INTERNAL").detail, {})

    def test_to_error_has_the_ui_contract_keys(self):
        payload = BridgeError("IO_ERROR", "disk", {"path": "C:/x"}).to_error()
        self.assertEqual(
            set(payload),
            {"code", "message_key", "detail", "retryable"},
        )
        self.assertEqual(payload["code"], "IO_ERROR")
        self.assertEqual(payload["message_key"], "error.IO_ERROR")
        self.assertTrue(payload["retryable"])
        self.assertEqual(payload["detail"]["path"], "C:/x")

    def test_helpers_build_the_expected_codes(self):
        self.assertEqual(invalid("bad").code, "INVALID_ARGUMENT")
        self.assertEqual(invalid("bad", field="x").detail["field"], "x")
        self.assertEqual(not_found("gone").code, "NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
