# -*- coding: utf-8 -*-
"""RFC 8628 polling state machine (T16), driven with a fake clock and client.

Every wait in :class:`DeviceFlow` is ``Event.wait(interval)``, so the tests
hand the flow a 10 ms interval and a fake monotonic clock; nothing here sleeps
for a real five seconds.
"""

import time
import unittest

from src.app.auth.config import load_providers
from src.app.auth.device_flow import (
    MAX_NETWORK_FAILURES,
    SLOW_DOWN_STEP,
    DeviceFlow,
    STATE_AUTHENTICATED,
    STATE_CANCELLED,
    STATE_EXPIRED,
    STATE_FAILED,
    STATE_PENDING,
)
from src.app.errors import BridgeError

TOKEN = "gho_fake_access_token_value"


class FakeClock(object):
    def __init__(self, start=0.0):
        self.now = start

    def __call__(self):
        return self.now


class FakeClient(object):
    """Returns queued payloads; an ``Exception`` raises instead."""

    def __init__(self, *results):
        self.results = list(results)
        self.polls = 0
        self.seen = []

    def poll_device_token(self, device_code):
        self.polls += 1
        self.seen.append(device_code)
        if self.results:
            result = self.results.pop(0)
        else:
            result = {"error": "authorization_pending"}
        if isinstance(result, Exception):
            raise result
        return result


def provider():
    return load_providers({"DISK_WARDEN_GITHUB_CLIENT_ID": "test-client"})["github"]


def device(**overrides):
    base = {
        "device_code": "device-code-value",
        "user_code": "WDJB-MJHT",
        "verification_uri": "https://github.com/login/device",
        "expires_in": 900,
        "interval": 0.01,
    }
    base.update(overrides)
    return base


def wait_for(flow, predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = flow.status()
        if predicate(status):
            return status
        time.sleep(0.01)
    return flow.status()


class FlowTestBase(unittest.TestCase):
    def build(self, client, opener=None, clock=None, **device_overrides):
        self.opened = []
        flow = DeviceFlow(
            provider(),
            device(**device_overrides),
            client,
            opener=opener if opener is not None else self.opened.append,
            monotonic=clock or FakeClock(),
        )
        self.addCleanup(flow.close)
        return flow


class StartTest(FlowTestBase):
    def test_start_opens_the_verification_page(self):
        flow = self.build(FakeClient())
        status = flow.status()
        self.assertEqual(status["state"], STATE_PENDING)
        self.assertEqual(status["user_code"], "WDJB-MJHT")
        self.assertEqual(status["verification_uri"], "https://github.com/login/device")
        self.assertNotIn("device_code", status)
        flow.start()
        self.assertEqual(self.opened, ["https://github.com/login/device"])

    def test_missing_browser_does_not_fail_the_flow(self):
        def opener(url):
            raise RuntimeError("no browser")

        flow = self.build(FakeClient({"error": "authorization_pending"}), opener=opener)
        flow.start()
        self.assertEqual(flow.status()["state"], STATE_PENDING)

    def test_status_exposes_the_countdown(self):
        clock = FakeClock(100.0)
        flow = self.build(FakeClient(), clock=clock, expires_in=600, interval=5)
        status = flow.status()
        self.assertEqual(status["expires_in"], 600)
        self.assertTrue(status["expires_at"].endswith("Z"))
        self.assertEqual(status["interval"], 5)


class SuccessTest(FlowTestBase):
    def test_pending_then_token_authenticates(self):
        client = FakeClient({"error": "authorization_pending"}, {"access_token": TOKEN})
        flow = self.build(client)
        seen = []
        flow.set_completion(lambda token, payload: (seen.append(token), flow.mark_authenticated({"login": "octocat"}, False)))
        flow.start()
        status = wait_for(flow, lambda s: s["state"] == STATE_AUTHENTICATED)
        self.assertEqual(status["state"], STATE_AUTHENTICATED)
        self.assertEqual(seen, [TOKEN])
        self.assertEqual(client.seen, ["device-code-value"] * client.polls)
        self.assertGreaterEqual(client.polls, 2)

    def test_completion_failure_marks_the_flow_failed(self):
        flow = self.build(FakeClient({"access_token": TOKEN}))

        def completion(token, payload):
            raise BridgeError("AUTH_FAILED", "profile")

        flow.set_completion(completion)
        flow.start()
        status = wait_for(flow, lambda s: s["state"] == STATE_FAILED)
        self.assertEqual(status["error"]["code"], "AUTH_FAILED")
        self.assertNotIn(TOKEN, str(status))


class ErrorTest(FlowTestBase):
    def test_slow_down_increases_the_interval(self):
        flow = self.build(
            FakeClient({"error": "slow_down"}, {"error": "slow_down"}), interval=1
        )
        flow.start()
        status = wait_for(flow, lambda s: s["interval"] > 1)
        self.assertEqual(status["interval"], 1 + int(SLOW_DOWN_STEP))
        self.assertLessEqual(status["interval"], 60)

    def test_access_denied_is_a_cancellation(self):
        flow = self.build(FakeClient({"error": "access_denied"}))
        flow.start()
        status = wait_for(flow, lambda s: s["state"] == STATE_FAILED)
        self.assertEqual(status["error"]["code"], "AUTH_CANCELLED")
        self.assertEqual(status["error"]["reason"], "access_denied")

    def test_expired_token_ends_the_flow(self):
        flow = self.build(FakeClient({"error": "expired_token"}))
        flow.start()
        status = wait_for(flow, lambda s: s["state"] == STATE_EXPIRED)
        self.assertEqual(status["state"], STATE_EXPIRED)

    def test_unknown_error_is_a_failure(self):
        flow = self.build(FakeClient({"error": "device_flow_disabled"}))
        flow.start()
        status = wait_for(flow, lambda s: s["state"] == STATE_FAILED)
        self.assertEqual(status["error"]["reason"], "device_flow_disabled")

    def test_deadline_expires_the_flow_without_polling(self):
        clock = FakeClock(0.0)
        client = FakeClient()
        flow = self.build(client, clock=clock, expires_in=30)
        clock.now = 100.0  # already past the deadline before polling starts
        flow.start()
        status = wait_for(flow, lambda s: s["state"] == STATE_EXPIRED)
        self.assertEqual(status["state"], STATE_EXPIRED)
        self.assertEqual(client.polls, 0)

    def test_repeated_network_failures_stop_the_flow(self):
        client = FakeClient(*[BridgeError("AUTH_FAILED", "offline")] * MAX_NETWORK_FAILURES)
        flow = self.build(client)
        flow.start()
        status = wait_for(flow, lambda s: s["state"] == STATE_FAILED)
        self.assertEqual(status["error"]["code"], "AUTH_FAILED")
        self.assertEqual(status["error"]["detail"]["attempts"], MAX_NETWORK_FAILURES)
        self.assertEqual(client.polls, MAX_NETWORK_FAILURES)

    def test_network_failures_reset_after_a_successful_poll(self):
        results = [BridgeError("AUTH_FAILED", "offline"), BridgeError("AUTH_FAILED", "offline")]
        results += [{"error": "authorization_pending"}] * 2
        results += [BridgeError("AUTH_FAILED", "offline"), BridgeError("AUTH_FAILED", "offline")]
        results += [{"access_token": TOKEN}]
        flow = self.build(FakeClient(*results))
        flow.set_completion(lambda token, payload: flow.mark_authenticated({"login": "x"}, False))
        flow.start()
        status = wait_for(flow, lambda s: s["state"] in (STATE_AUTHENTICATED, STATE_FAILED))
        self.assertEqual(status["state"], STATE_AUTHENTICATED)


class CancelTest(FlowTestBase):
    def test_cancel_ends_the_flow(self):
        flow = self.build(FakeClient())
        flow.start()
        flow.cancel()
        status = flow.status()
        self.assertEqual(status["state"], STATE_CANCELLED)
        self.assertIsNone(status["error"])

    def test_authenticated_cannot_be_cancelled(self):
        flow = self.build(FakeClient())
        flow.mark_authenticated({"login": "octocat"}, False)
        flow.cancel()
        self.assertEqual(flow.status()["state"], STATE_AUTHENTICATED)

    def test_close_is_idempotent(self):
        flow = self.build(FakeClient())
        flow.start()
        flow.close()
        flow.close()
        self.assertEqual(flow.status()["state"], STATE_CANCELLED)


if __name__ == "__main__":
    unittest.main()
