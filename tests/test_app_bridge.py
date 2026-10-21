# -*- coding: utf-8 -*-
"""JSON-RPC whitelist and argument validation (architecture §5.1)."""

import json
import unittest
from unittest import mock

from src.app import PROTOCOL_VERSION
from src.app.bridge import Bridge, MAX_REQUEST_BYTES, build_methods, handle_request
from src.app.errors import BridgeError

EXPECTED_METHODS = {
    "app.hello",
    "app.ping",
    "app.reveal",
    "app.open_external",
    "settings.get",
    "settings.set",
    "ai.status",
    "ai.consent",
    "ai.test",
    "auth.status",
    "auth.start",
    "auth.cancel",
    "auth.sign_out",
    "welcome.seen",
    "star.show",
    "star.dismiss",
    "star.give",
    "star.check",
    "disk.info",
    "scan.start",
    "scan.cancel",
    "scan.status",
    "scan.result",
    "items.list",
    "plan.preview",
    "migrate.start",
    "job.status",
    "job.cancel",
    "job.list",
    "rollback.preview",
    "rollback.start",
    "recovery.list",
    "recovery.run",
    "recovery.ignore",
    "history.list",
    "rules.learned.list",
    "rules.override",
    "rules.override.clear",
    "events.poll",
}


class BridgeTestBase(unittest.TestCase):
    def setUp(self):
        self.service = mock.Mock()
        self.service.plan_preview.return_value = {"plan_id": "p1"}
        self.methods = build_methods(self.service)

    def call(self, method, params=None, request_id="1"):
        request = {"v": PROTOCOL_VERSION, "id": request_id, "method": method}
        if params is not None:
            request["params"] = params
        return handle_request(self.service, request, self.methods)

    def assert_error(self, response, code):
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], code)
        self.assertEqual(response["error"]["message_key"], "error." + code)
        self.assertNotIn("result", response)
        return response["error"]


class WhitelistTest(BridgeTestBase):
    def test_method_table_is_exact(self):
        self.assertEqual(set(self.methods), EXPECTED_METHODS)
        for handler in self.methods.values():
            self.assertTrue(callable(handler))

    def test_unknown_method_is_rejected_without_dispatch(self):
        response = self.call("system.exec", {"path": "C:/Windows"})
        error = self.assert_error(response, "INVALID_ARGUMENT")
        self.assertEqual(error["detail"]["method"], "system.exec")
        self.assertEqual(response["id"], "1")

    def test_missing_method_is_rejected(self):
        response = handle_request(
            self.service, {"v": PROTOCOL_VERSION, "id": "2"}, self.methods
        )
        self.assert_error(response, "INVALID_ARGUMENT")

    def test_non_object_request_is_rejected(self):
        for payload in ([], "5", None, True):
            response = handle_request(self.service, payload, self.methods)
            self.assert_error(response, "INVALID_ARGUMENT")

    def test_invalid_json_is_rejected(self):
        response = handle_request(self.service, "{nope", self.methods)
        error = self.assert_error(response, "INVALID_ARGUMENT")
        self.assertIsNone(response["id"])
        self.assertIn("JSON", error["detail"]["reason"])

    def test_protocol_version_mismatch_is_rejected(self):
        response = handle_request(
            self.service,
            {"v": PROTOCOL_VERSION + 1, "id": "3", "method": "app.ping"},
            self.methods,
        )
        error = self.assert_error(response, "INVALID_ARGUMENT")
        self.assertEqual(error["detail"]["expected"], PROTOCOL_VERSION)
        self.assertEqual(response["id"], "3")

    def test_ping_does_not_touch_the_service(self):
        response = self.call("app.ping")
        self.assertTrue(response["ok"])
        self.assertEqual(
            response["result"], {"ok": True, "protocol": PROTOCOL_VERSION}
        )

    def test_reveal_requires_a_path(self):
        error = self.assert_error(self.call("app.reveal", {}), "INVALID_ARGUMENT")
        self.assertIn("path", error["detail"]["reason"])
        self.service.reveal.assert_not_called()

    def test_reveal_forwards_the_path(self):
        self.service.reveal.return_value = {"opened": True, "path": "C:\\Windows"}
        response = self.call("app.reveal", {"path": "C:\\Windows"})
        self.assertTrue(response["ok"])
        self.assertEqual(
            response["result"], {"opened": True, "path": "C:\\Windows"}
        )
        self.service.reveal.assert_called_once_with("C:\\Windows")

    def test_open_external_requires_a_url(self):
        error = self.assert_error(
            self.call("app.open_external", {}), "INVALID_ARGUMENT"
        )
        self.assertIn("url", error["detail"]["reason"])
        self.service.open_external.assert_not_called()

    def test_open_external_forwards_the_url(self):
        url = "https://github.com/QuotaFuse-ai/disk-warden/issues/new"
        self.service.open_external.return_value = {"opened": True, "url": url}
        response = self.call("app.open_external", {"url": url})
        self.assertTrue(response["ok"])
        self.service.open_external.assert_called_once_with(url)

    def test_override_clear_requires_an_item_id(self):
        error = self.assert_error(
            self.call("rules.override.clear", {}), "INVALID_ARGUMENT"
        )
        self.assertIn("item_id", error["detail"]["reason"])
        self.service.rules_override_clear.assert_not_called()

    def test_override_clear_forwards_the_item_id(self):
        self.call("rules.override.clear", {"item_id": "cursor-cache"})
        self.service.rules_override_clear.assert_called_once_with("cursor-cache")


class ParamsValidationTest(BridgeTestBase):
    def test_params_must_be_an_object(self):
        response = self.call("app.ping", [])
        self.assert_error(response, "INVALID_ARGUMENT")

    def test_plan_preview_requires_item_ids(self):
        error = self.assert_error(self.call("plan.preview", {}), "INVALID_ARGUMENT")
        self.assertIn("item_ids", error["detail"]["reason"])
        self.service.plan_preview.assert_not_called()

    def test_plan_preview_rejects_non_string_ids(self):
        for value in ("abc", [1, 2], ["ok", 3], ["ok", None]):
            error = self.assert_error(
                self.call("plan.preview", {"item_ids": value}),
                "INVALID_ARGUMENT",
            )
            self.assertIn("item_ids", error["detail"]["reason"])
        self.service.plan_preview.assert_not_called()

    def test_plan_preview_forwards_confirmed_ids(self):
        self.call(
            "plan.preview",
            {"item_ids": ["a", "b"], "confirmed_item_ids": ["b"]},
        )
        self.service.plan_preview.assert_called_once_with(["a", "b"], ["b"])

    def test_confirmed_item_ids_defaults_to_empty(self):
        self.call("plan.preview", {"item_ids": ["a"]})
        self.service.plan_preview.assert_called_once_with(["a"], [])

    def test_migrate_start_requires_plan_and_token(self):
        error = self.assert_error(
            self.call("migrate.start", {"plan_id": "p1"}), "INVALID_ARGUMENT"
        )
        self.assertIn("confirm_token", error["detail"]["reason"])
        self.assert_error(self.call("migrate.start", {}), "INVALID_ARGUMENT")
        self.service.migrate_start.assert_not_called()

    def test_migrate_start_forwards_confirmed_ids(self):
        self.call(
            "migrate.start",
            {
                "plan_id": "p1",
                "confirm_token": "t1",
                "confirmed_item_ids": ["a"],
            },
        )
        self.service.migrate_start.assert_called_once_with("p1", "t1", ["a"])

    def test_required_strings_reject_empty_and_wrong_types(self):
        cases = (
            ("job.status", {}),
            ("job.status", {"job_id": ""}),
            ("job.status", {"job_id": 7}),
            ("job.cancel", {}),
            ("rollback.preview", {}),
            ("rollback.start", {"plan_id": "p1"}),
            ("rules.override", {"item_id": "a"}),
            ("rules.override.clear", {}),
            ("rules.override.clear", {"item_id": 7}),
            ("app.open_external", {"url": ""}),
            ("app.open_external", {"url": 7}),
        )
        for method, params in cases:
            error = self.assert_error(
                self.call(method, params), "INVALID_ARGUMENT"
            )
            self.assertTrue(error["detail"].get("reason"))
        for name in ("job_status", "job_cancel", "rollback_preview"):
            self.assertFalse(getattr(self.service, name).called)
        self.service.rules_override.assert_not_called()
        self.service.rules_override_clear.assert_not_called()
        self.service.open_external.assert_not_called()

    def test_optional_bool_rejects_non_booleans(self):
        error = self.assert_error(
            self.call("job.list", {"active_only": "yes"}), "INVALID_ARGUMENT"
        )
        self.assertIn("active_only", error["detail"]["reason"])
        self.service.job_list.assert_not_called()

    def test_optional_bool_forwards_value(self):
        self.call("job.list", {"active_only": True})
        self.service.job_list.assert_called_once_with(True)

    def test_recovery_run_action_defaults_to_recover(self):
        self.call("recovery.run", {"job_id": "abc"})
        self.call("recovery.run", {"job_id": "abc", "action": ""})
        self.call("recovery.run", {"job_id": "abc", "action": "rollback"})
        calls = self.service.recovery_run.call_args_list
        self.assertEqual([call[0] for call in calls], [
            ("abc", "recover"),
            ("abc", "recover"),
            ("abc", "rollback"),
        ])

    def test_recovery_ignore_requires_a_job_id(self):
        error = self.assert_error(
            self.call("recovery.ignore", {}), "INVALID_ARGUMENT"
        )
        self.assertIn("job_id", error["detail"]["reason"])
        self.service.recovery_ignore.assert_not_called()
        self.call("recovery.ignore", {"job_id": "abc"})
        self.service.recovery_ignore.assert_called_once_with("abc")

    def test_history_list_defaults(self):
        self.call("history.list", {})
        self.service.history_list.assert_called_once_with("all", 500)

    def test_settings_set_uses_patch_object(self):
        self.service.settings_set.return_value = {}
        self.call(
            "settings.set",
            {"patch": {"language": "zh-CN"}, "language": "ignored"},
        )
        self.service.settings_set.assert_called_once_with({"language": "zh-CN"})

    def test_settings_set_flattens_bare_params(self):
        self.service.settings_set.return_value = {}
        self.call(
            "settings.set",
            {"language": "zh-CN", "launch_at_startup": True},
        )
        self.service.settings_set.assert_called_once_with(
            {"language": "zh-CN", "launch_at_startup": True}
        )


class DispatchErrorTest(BridgeTestBase):
    def test_bridge_error_is_returned_verbatim(self):
        self.service.job_status.side_effect = BridgeError(
            "NOT_FOUND", "job not found", {"job_id": "abc"}
        )
        response = self.call("job.status", {"job_id": "abc"})
        error = self.assert_error(response, "NOT_FOUND")
        self.assertEqual(error["detail"]["job_id"], "abc")
        self.assertFalse(error["retryable"])

    def test_unexpected_exception_maps_to_internal(self):
        self.service.disk_info.side_effect = RuntimeError("boom")
        error = self.assert_error(self.call("disk.info"), "INTERNAL")
        self.assertEqual(error["detail"]["reason"], "RuntimeError: boom")
        self.assertFalse(error["retryable"])

    def test_success_envelope_shape(self):
        self.service.scan_start.return_value = {"job_id": "j1"}
        response = self.call("scan.start")
        self.assertEqual(response["v"], PROTOCOL_VERSION)
        self.assertEqual(response["id"], "1")
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"], {"job_id": "j1"})


class BridgeRpcTest(unittest.TestCase):
    def setUp(self):
        self.service = mock.Mock()
        self.bridge = Bridge(self.service)

    def _rpc(self, request):
        return json.loads(self.bridge.rpc(json.dumps(request)))

    def test_rpc_round_trip(self):
        response = self._rpc({"v": PROTOCOL_VERSION, "id": "9", "method": "app.ping"})
        self.assertTrue(response["ok"])
        self.assertEqual(response["id"], "9")

    def test_oversize_request_is_rejected_before_parsing(self):
        response = json.loads(self.bridge.rpc("x" * (MAX_REQUEST_BYTES + 1)))
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "INVALID_ARGUMENT")
        self.assertIsNone(response["id"])

    def test_unserialisable_result_falls_back_to_internal(self):
        self.service.disk_info.return_value = object()
        response = self._rpc(
            {"v": PROTOCOL_VERSION, "id": "10", "method": "disk.info"}
        )
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "INTERNAL")
        self.assertEqual(response["id"], "10")

    def test_to_dict_results_are_serialised(self):
        class Payload(object):
            def to_dict(self):
                return {"answer": 42}

        self.service.disk_info.return_value = Payload()
        response = self._rpc(
            {"v": PROTOCOL_VERSION, "id": "11", "method": "disk.info"}
        )
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"], {"answer": 42})


if __name__ == "__main__":
    unittest.main()
