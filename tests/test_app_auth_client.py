# -*- coding: utf-8 -*-
"""GitHub device-flow HTTP calls with an injected opener (T16).

Nothing here touches the network.  The guarantees under test are: only the
public client id is sent (there is no secret in this flow at all), the scope
stays ``read:user``, every URL is checked against the host allow-list, and no
error message ever echoes the token.
"""

import io
import json
import unittest
from dataclasses import replace
from urllib.error import HTTPError, URLError

from src.app.auth.client import (
    DEVICE_GRANT_TYPE,
    MAX_RESPONSE_BYTES,
    OAuthClient,
)
from src.app.auth.config import load_providers, star_url
from src.app.errors import BridgeError

FAKE_ID = "Iv1.fake-client-id-for-tests"
TOKEN = "gho_fake_access_token_value"

DEVICE_BODY = {
    "device_code": "device-code-value",
    "user_code": "WDJB-MJHT",
    "verification_uri": "https://github.com/login/device",
    "expires_in": 900,
    "interval": 5,
}


class FakeResponse(object):
    def __init__(self, status, body=b""):
        self.status = status
        self._body = body
        self.closed = False
        self.limit = None

    def read(self, limit=None):
        self.limit = limit
        return self._body if limit is None else self._body[:limit]

    def close(self):
        self.closed = True


class Recorder(object):
    """Opener stub: returns queued responses and records each request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    @property
    def last(self):
        return self.requests[-1][0]


def provider(**env):
    base = {"DISK_WARDEN_GITHUB_CLIENT_ID": FAKE_ID}
    base.update(env)
    return load_providers(base)["github"]


def json_response(payload, status=200):
    return FakeResponse(status, json.dumps(payload).encode("utf-8"))


class DeviceCodeTest(unittest.TestCase):
    def test_request_posts_the_public_id_and_reads_scope(self):
        recorder = Recorder(json_response(DEVICE_BODY))
        device = OAuthClient(provider(), opener=recorder).request_device_code()
        self.assertEqual(device["device_code"], "device-code-value")
        self.assertEqual(device["user_code"], "WDJB-MJHT")
        self.assertEqual(device["verification_uri"], "https://github.com/login/device")
        self.assertEqual(device["expires_in"], 900)
        self.assertEqual(device["interval"], 5)
        body = recorder.last.data.decode("ascii")
        self.assertIn("client_id=%s" % FAKE_ID, body)
        self.assertIn("scope=read%3Auser", body)
        self.assertNotIn("secret", body.lower())
        self.assertEqual(recorder.last.get_method(), "POST")
        self.assertEqual(recorder.last.full_url, "https://github.com/login/device/code")

    def test_form_encoded_answer_is_accepted(self):
        body = b"device_code=d&user_code=U&verification_uri=https%3A%2F%2Fgithub.com%2Flogin%2Fdevice&expires_in=600&interval=7"
        device = OAuthClient(provider(), opener=Recorder(FakeResponse(200, body))).request_device_code()
        self.assertEqual(device["device_code"], "d")
        self.assertEqual(device["interval"], 7)
        self.assertEqual(device["expires_in"], 600)

    def test_missing_timing_fields_fall_back_to_defaults(self):
        payload = {"device_code": "d", "user_code": "U", "verification_uri": "https://x"}
        device = OAuthClient(provider(), opener=Recorder(json_response(payload))).request_device_code()
        self.assertEqual(device["interval"], 5)
        self.assertEqual(device["expires_in"], 900)

    def test_error_payload_is_a_failure(self):
        client = OAuthClient(
            provider(), opener=Recorder(json_response({"error": "device_flow_disabled"}))
        )
        with self.assertRaises(BridgeError) as ctx:
            client.request_device_code()
        self.assertEqual(ctx.exception.code, "AUTH_FAILED")
        self.assertEqual(ctx.exception.detail["stage"], "device_code")
        self.assertEqual(ctx.exception.detail["reason"], "device_flow_disabled")

    def test_incomplete_response_is_a_failure(self):
        client = OAuthClient(provider(), opener=Recorder(json_response({"user_code": "U"})))
        with self.assertRaises(BridgeError) as ctx:
            client.request_device_code()
        self.assertEqual(ctx.exception.code, "AUTH_FAILED")
        self.assertEqual(ctx.exception.detail["stage"], "device_code")


class PollTest(unittest.TestCase):
    def poll(self, payload, status=200):
        client = OAuthClient(provider(), opener=Recorder(json_response(payload, status)))
        return client.poll_device_token("device-code-value")

    def test_token_is_returned(self):
        payload = self.poll({"access_token": TOKEN, "token_type": "bearer"})
        self.assertEqual(payload["access_token"], TOKEN)

    def test_request_carries_the_device_grant(self):
        recorder = Recorder(json_response({"error": "authorization_pending"}))
        OAuthClient(provider(), opener=recorder).poll_device_token("device-code-value")
        body = recorder.last.data.decode("ascii")
        self.assertIn("device_code=device-code-value", body)
        self.assertIn("client_id=%s" % FAKE_ID, body)
        self.assertIn("grant_type=%s" % DEVICE_GRANT_TYPE.replace(":", "%3A").replace("/", "%2F"), body)
        self.assertEqual(recorder.last.full_url, "https://github.com/login/oauth/access_token")

    def test_pending_and_slow_down_are_passed_through(self):
        for error in ("authorization_pending", "slow_down"):
            payload = self.poll({"error": error})
            self.assertEqual(payload["error"], error)
            self.assertNotIn("access_token", payload)

    def test_terminal_errors_are_passed_through(self):
        for error in ("expired_token", "access_denied"):
            self.assertEqual(self.poll({"error": error})["error"], error)

    def test_unlabelled_body_is_a_failure(self):
        with self.assertRaises(BridgeError) as ctx:
            self.poll({})
        self.assertEqual(ctx.exception.code, "AUTH_FAILED")
        self.assertEqual(ctx.exception.detail["stage"], "device_poll")

    def test_server_error_is_a_failure(self):
        with self.assertRaises(BridgeError) as ctx:
            self.poll({"message": "boom"}, status=503)
        self.assertEqual(ctx.exception.code, "AUTH_FAILED")
        self.assertEqual(ctx.exception.detail["reason"], "network")
        self.assertTrue(ctx.exception.retryable)

    def test_empty_body_is_a_failure(self):
        client = OAuthClient(provider(), opener=Recorder(FakeResponse(200, b"")))
        with self.assertRaises(BridgeError) as ctx:
            client.poll_device_token("device-code-value")
        self.assertEqual(ctx.exception.detail["reason"], "network")

    def test_errors_never_echo_the_token(self):
        client = OAuthClient(provider(), opener=Recorder(FakeResponse(500, b"{}")))
        with self.assertRaises(BridgeError) as ctx:
            client.poll_device_token(TOKEN)
        self.assertNotIn(TOKEN, repr(ctx.exception.detail))


class ProfileTest(unittest.TestCase):
    def test_profile_returns_login_name_and_avatar(self):
        recorder = Recorder(
            json_response(
                {
                    "login": "octocat",
                    "name": "Mona",
                    "avatar_url": "https://avatars.githubusercontent.com/u/1",
                }
            )
        )
        user = OAuthClient(provider(), opener=recorder).fetch_user(TOKEN)
        self.assertEqual(user["login"], "octocat")
        self.assertEqual(user["name"], "Mona")
        self.assertTrue(user["avatar_url"].startswith("https://avatars."))
        self.assertEqual(user["provider"], "github")
        self.assertEqual(recorder.last.get_header("Authorization"), "Bearer %s" % TOKEN)

    def test_rejected_token_is_auth_required(self):
        client = OAuthClient(provider(), opener=Recorder(FakeResponse(401)))
        with self.assertRaises(BridgeError) as ctx:
            client.fetch_user(TOKEN)
        self.assertEqual(ctx.exception.code, "AUTH_REQUIRED")
        self.assertEqual(ctx.exception.detail["status"], 401)

    def test_profile_without_login_is_a_failure(self):
        client = OAuthClient(provider(), opener=Recorder(json_response({})))
        with self.assertRaises(BridgeError) as ctx:
            client.fetch_user(TOKEN)
        self.assertEqual(ctx.exception.code, "AUTH_FAILED")
        self.assertEqual(ctx.exception.detail["stage"], "profile")

    def test_http_error_does_not_leak_the_token(self):
        error = HTTPError(
            "https://api.github.com/user", 500, "boom", {}, io.BytesIO(b'{"message":"boom"}')
        )
        client = OAuthClient(provider(), opener=Recorder(error))
        with self.assertRaises(BridgeError) as ctx:
            client.fetch_user(TOKEN)
        self.assertEqual(ctx.exception.code, "AUTH_FAILED")
        self.assertNotIn(TOKEN, repr(ctx.exception.detail))


class StarTest(unittest.TestCase):
    def test_check_starred_true_and_false(self):
        for status, expected in ((204, True), (404, False)):
            client = OAuthClient(provider(), opener=Recorder(FakeResponse(status)))
            self.assertEqual(client.check_starred(TOKEN, "octocat"), expected)

    def test_public_star_endpoint_is_used(self):
        recorder = Recorder(FakeResponse(404))
        OAuthClient(provider(), opener=recorder).check_starred(TOKEN, "octocat")
        self.assertEqual(
            recorder.last.full_url,
            "https://api.github.com/users/octocat/starred/QuotaFuse-ai/disk-warden",
        )
        self.assertEqual(recorder.last.get_method(), "GET")

    def test_check_starred_reports_bad_status(self):
        client = OAuthClient(provider(), opener=Recorder(FakeResponse(500)))
        with self.assertRaises(BridgeError) as ctx:
            client.check_starred(TOKEN, "octocat")
        self.assertEqual(ctx.exception.code, "AUTH_FAILED")
        self.assertEqual(ctx.exception.detail["stage"], "star_status")

    def test_check_starred_maps_403_to_auth_required(self):
        client = OAuthClient(provider(), opener=Recorder(FakeResponse(403)))
        with self.assertRaises(BridgeError) as ctx:
            client.check_starred(TOKEN, "octocat")
        self.assertEqual(ctx.exception.code, "AUTH_REQUIRED")

    def test_login_is_percent_encoded(self):
        self.assertIn("/users/octo%20cat/starred/", star_url("octo cat"))


class TransportTest(unittest.TestCase):
    def test_network_error_becomes_a_bridge_error(self):
        client = OAuthClient(provider(), opener=Recorder(URLError("offline")))
        with self.assertRaises(BridgeError) as ctx:
            client.fetch_user(TOKEN)
        self.assertEqual(ctx.exception.code, "AUTH_FAILED")
        self.assertTrue(ctx.exception.retryable)

    def test_response_is_bounded(self):
        response = FakeResponse(200, b'{"login":"x"}')
        OAuthClient(provider(), opener=Recorder(response)).fetch_user(TOKEN)
        self.assertEqual(response.limit, MAX_RESPONSE_BYTES)

    def test_hosts_outside_the_allow_list_are_refused(self):
        rogue = replace(provider(), device_code_url="https://evil.example/login/device/code")
        client = OAuthClient(rogue, opener=Recorder(json_response(DEVICE_BODY)))
        with self.assertRaises(BridgeError) as ctx:
            client.request_device_code()
        self.assertEqual(ctx.exception.detail["reason"], "blocked_host")
        self.assertEqual(ctx.exception.detail["host"], "evil.example")

    def test_allow_list_covers_github_only(self):
        from src.app.auth.config import ALLOWED_HOSTS

        self.assertEqual(set(ALLOWED_HOSTS), {"github.com", "api.github.com"})


if __name__ == "__main__":
    unittest.main()
