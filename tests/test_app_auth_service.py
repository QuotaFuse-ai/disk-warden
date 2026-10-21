# -*- coding: utf-8 -*-
"""Service-level auth wiring: RPC envelope, persistence, end-to-end flow.

This is the closest thing to the acceptance script for T16: the welcome page
can read provider state, a device code can be requested and polled through the
injected HTTP opener, and a successful sign-in never puts a token in
``auth.json`` or in any payload sent to the UI.
"""

import json
import os
import time
import unittest
from dataclasses import replace

from src.app.auth.config import REPO_WEB_URL, load_providers
from src.app.errors import BridgeError

from tests.app_fakes import make_service

FAKE_ID = "Iv1.fake-client-id-for-tests"
TOKEN = "gho_fake_access_token_value"
GITHUB_ENV = {"DISK_WARDEN_GITHUB_CLIENT_ID": FAKE_ID}

USER_CODE = "WDJB-MJHT"
VERIFY_URI = "https://github.com/login/device"


class FakeResponse(object):
    def __init__(self, status, body=b""):
        self.status = status
        self._body = body

    def read(self, limit=None):
        return self._body if limit is None else self._body[:limit]

    def close(self):
        pass


def json_response(payload, status=200):
    return FakeResponse(status, json.dumps(payload).encode("utf-8"))


class FakeGitHub(object):
    """Routes by URL; no queue, so poll ordering cannot race the test."""

    def __init__(self, interval=1, pending_rounds=0, star_status=404):
        self.interval = interval
        self.pending_rounds = pending_rounds
        self.star_status = star_status
        self.calls = []
        self.polls = 0

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.calls.append((request.get_method(), url))
        if url.endswith("/login/device/code"):
            return json_response(
                {
                    "device_code": "device-code-value",
                    "user_code": USER_CODE,
                    "verification_uri": VERIFY_URI,
                    "expires_in": 900,
                    "interval": self.interval,
                }
            )
        if url.endswith("/login/oauth/access_token"):
            self.polls += 1
            if self.polls <= self.pending_rounds:
                return json_response({"error": "authorization_pending"})
            return json_response(
                {"access_token": TOKEN, "token_type": "bearer", "scope": "read:user"}
            )
        if url.endswith("api.github.com/user"):
            return json_response(
                {
                    "login": "octocat",
                    "name": "Mona",
                    "avatar_url": "https://avatars.githubusercontent.com/u/1",
                }
            )
        if "/starred/" in url:
            return FakeResponse(self.star_status)
        return FakeResponse(404)


def wait_for_signed_in(service, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = service.auth_status()
        flow = status["flow"] or {}
        if status["signed_in"] or flow.get("state") in ("failed", "cancelled", "expired"):
            return status
        time.sleep(0.02)
    return service.auth_status()


class AuthEnvelopeTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service(self, environ={})

    def test_status_reports_one_device_flow_provider(self):
        status = self.service.auth_status()
        self.assertFalse(status["signed_in"])
        self.assertIsNone(status["login"])
        self.assertFalse(status["welcome_seen"])
        self.assertIsNone(status["flow"])
        self.assertEqual(status["star"], {"pending": False, "dismissed": False, "starred": False})
        by_id = {item["provider"]: item for item in status["providers"]}
        self.assertEqual(set(by_id), {"github"})
        github = by_id["github"]
        self.assertTrue(github["configured"])
        self.assertEqual(github["flow"], "device_code")
        self.assertTrue(github["device_flow"])
        self.assertEqual(github["scopes"], ["read:user"])
        self.assertEqual(github["client_id_env"], "DISK_WARDEN_GITHUB_CLIENT_ID")
        self.assertNotIn("client_id", github)
        self.assertNotIn("client_secret", github)

    def test_gitee_is_a_link_only(self):
        gitee = self.service.auth_status()["gitee"]
        self.assertEqual(gitee["provider"], "gitee")
        self.assertFalse(gitee["available"])
        self.assertTrue(gitee["url"].startswith("https://gitee.com/"))
        self.assertNotIn("client_id", gitee)

    def test_every_auth_method_returns_the_same_envelope(self):
        status = self.service.auth_status()
        for payload in (
            self.service.welcome_seen(),
            self.service.star_show(),
            self.service.star_dismiss(),
            self.service.auth_cancel(),
            self.service.auth_sign_out(),
        ):
            self.assertEqual(set(payload), set(status))
            self.assertEqual(payload["repository_url"], REPO_WEB_URL)
            self.assertIn("gitee", payload)

    def test_hello_carries_the_auth_block(self):
        hello = self.service.hello()
        self.assertIn("auth", hello)
        self.assertEqual(hello["auth"]["repository_url"], REPO_WEB_URL)

    def test_payload_contains_no_credentials(self):
        blob = json.dumps(self.service.auth_status())
        self.assertNotIn(FAKE_ID, blob)
        self.assertNotIn('"client_secret"', blob)
        self.assertNotIn('"access_token"', blob)

    def test_start_rejects_an_unknown_provider(self):
        for value in ("", None, 42, "gitee"):
            with self.assertRaises(BridgeError) as ctx:
                self.service.auth_start(value)
            self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")

    def test_start_without_a_client_id_reports_the_blocker(self):
        blank = replace(load_providers({})["github"], client_id="")
        service = make_service(
            self,
            environ={},
            auth_provider_loader=lambda env: {"github": blank},
        )
        self.assertFalse(service.auth_status()["providers"][0]["configured"])
        with self.assertRaises(BridgeError) as ctx:
            service.auth_start("github")
        self.assertEqual(ctx.exception.code, "AUTH_NOT_CONFIGURED")
        self.assertEqual(ctx.exception.detail["env"], "DISK_WARDEN_GITHUB_CLIENT_ID")

    def test_star_requires_a_login(self):
        for call in (self.service.star_give, self.service.star_check):
            with self.assertRaises(BridgeError) as ctx:
                call()
            self.assertEqual(ctx.exception.code, "AUTH_REQUIRED")


class AuthPersistenceTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service(self, environ={})
        self.config_dir = self.service.layout.config_dir

    def test_welcome_seen_is_persisted(self):
        self.assertFalse(self.service.auth_status()["welcome_seen"])
        self.assertTrue(self.service.welcome_seen()["welcome_seen"])
        path = os.path.join(self.config_dir, "auth.json")
        self.assertTrue(os.path.exists(path))
        with open(path, "r", encoding="utf-8") as handle:
            stored = json.load(handle)
        self.assertTrue(stored["welcome_seen"])
        self.assertNotIn("access_token", stored)

    def test_star_prompt_can_be_reopened_and_dismissed(self):
        self.assertTrue(self.service.star_show()["star"]["pending"])
        dismissed = self.service.star_dismiss()["star"]
        self.assertFalse(dismissed["pending"])
        self.assertTrue(dismissed["dismissed"])
        self.assertTrue(self.service.star_show()["star"]["pending"])

    def test_sign_out_clears_login_and_star_state(self):
        status = self.service.auth_sign_out()
        self.assertFalse(status["signed_in"])
        self.assertFalse(status["star"]["pending"])
        self.assertFalse(status["star"]["dismissed"])


class ConfiguredFlowTest(unittest.TestCase):
    def setUp(self):
        self.opened = []
        self.github = FakeGitHub()
        self.service = make_service(
            self,
            environ=dict(GITHUB_ENV),
            auth_opener=self.opened.append,
            auth_http_opener=self.github,
        )

    def start(self):
        status = self.service.auth_start("github")
        self.assertEqual(status["flow"]["state"], "pending")
        self.assertEqual(status["flow"]["user_code"], USER_CODE)
        self.assertNotIn("device_code", status["flow"])
        self.assertEqual(self.opened, [VERIFY_URI])
        return status

    def sign_in(self):
        self.start()
        final = wait_for_signed_in(self.service)
        self.assertTrue(final["signed_in"], final)
        return final

    def test_start_exposes_only_the_user_code(self):
        status = self.start()
        flow = status["flow"]
        self.assertEqual(flow["provider"], "github")
        self.assertEqual(flow["verification_uri"], VERIFY_URI)
        self.assertGreater(flow["expires_in"], 0)
        self.assertTrue(flow["expires_at"].endswith("Z"))
        self.assertIsNone(flow["error"])

    def test_cancel_releases_the_flow(self):
        self.start()
        status = self.service.auth_cancel()
        self.assertEqual(status["flow"]["state"], "cancelled")
        self.assertFalse(status["signed_in"])

    def test_full_round_trip_signs_in_and_arms_the_star_prompt(self):
        final = self.sign_in()
        self.assertEqual(final["login"]["login"], "octocat")
        self.assertEqual(final["login"]["name"], "Mona")
        self.assertEqual(final["login"]["provider"], "github")
        self.assertFalse(final["login"]["starred"])
        self.assertTrue(final["star"]["pending"])
        self.assertNotIn(TOKEN, json.dumps(final))
        self.assertEqual(final["flow"]["state"], "authenticated")

        config_dir = self.service.layout.config_dir
        with open(os.path.join(config_dir, "auth.json"), "r", encoding="utf-8") as handle:
            stored = json.load(handle)
        self.assertEqual(stored["login"]["login"], "octocat")
        self.assertNotIn(TOKEN, json.dumps(stored))

        # Every call stayed on the two allowed hosts and never used a write.
        self.assertTrue(
            all(
                url.startswith(("https://github.com/", "https://api.github.com/"))
                for _method, url in self.github.calls
            )
        )
        self.assertNotIn("PUT", [method for method, _url in self.github.calls])

    def test_poll_waits_while_authorization_is_pending(self):
        self.github.pending_rounds = 1
        final = self.sign_in()
        self.assertEqual(self.github.polls, 2)
        self.assertTrue(final["signed_in"])

    def test_star_button_opens_the_repo_and_rereads_public_state(self):
        self.sign_in()
        self.github.star_status = 204
        self.opened[:] = []
        starred = self.service.star_give()
        self.assertEqual(self.opened, [REPO_WEB_URL])
        self.assertTrue(starred["star"]["starred"])
        self.assertFalse(starred["star"]["pending"])
        self.assertEqual(self.github.calls[-1][0], "GET")
        self.assertIn("/users/octocat/starred/QuotaFuse-ai/disk-warden", self.github.calls[-1][1])

    def test_star_check_alone_does_not_open_a_browser(self):
        self.sign_in()
        self.github.star_status = 204
        self.opened[:] = []
        status = self.service.star_check()
        self.assertTrue(status["star"]["starred"])
        self.assertEqual(self.opened, [])

    def test_sign_out_after_login(self):
        self.sign_in()
        signed_out = self.service.auth_sign_out()
        self.assertFalse(signed_out["signed_in"])
        self.assertFalse(
            os.path.exists(os.path.join(self.service.layout.config_dir, "auth-token.bin"))
        )

    def test_token_file_is_protected_or_absent(self):
        self.sign_in()
        config_dir = self.service.layout.config_dir
        path = os.path.join(config_dir, "auth-token.bin")
        if self.service.auth_status()["secure_storage"]:
            self.assertTrue(os.path.exists(path))
            with open(path, "rb") as handle:
                raw = handle.read()
            self.assertNotIn(TOKEN.encode(), raw)
        else:
            self.assertFalse(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
