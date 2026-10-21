# -*- coding: utf-8 -*-
"""Public configuration of the GitHub device flow (T16, second stage)."""

import unittest

from src.app.auth import config as auth_config


class DefaultsTest(unittest.TestCase):
    def test_builtin_client_id_is_shipped(self):
        providers = auth_config.load_providers({})
        github = providers["github"]
        self.assertEqual(github.client_id, "Ov23liZtqNoNtCAt8X8h")
        self.assertEqual(github.client_id_source, "builtin")
        self.assertTrue(github.configured)
        self.assertEqual(github.disabled_reason, "")

    def test_only_github_is_a_provider(self):
        self.assertEqual(auth_config.PROVIDER_IDS, ("github",))
        providers = auth_config.load_providers({})
        self.assertEqual(sorted(providers), ["github"])
        self.assertNotIn("gitee", providers)

    def test_device_flow_is_announced(self):
        public = auth_config.load_providers({})["github"].public_dict()
        self.assertEqual(public["flow"], "device_code")
        self.assertTrue(public["device_flow"])
        self.assertEqual(public["scopes"], ["read:user"])
        self.assertEqual(public["provider"], "github")

    def test_public_dict_never_carries_credentials(self):
        public = auth_config.load_providers({})["github"].public_dict()
        for key in ("client_id", "client_secret", "token", "device_code"):
            self.assertNotIn(key, public)
        self.assertEqual(public["client_id_env"], "DISK_WARDEN_GITHUB_CLIENT_ID")

    def test_config_has_no_secret_field_at_all(self):
        fields = auth_config.OAuthProviderConfig.__dataclass_fields__
        self.assertNotIn("client_secret", fields)
        self.assertNotIn("client_secret_env", fields)

    def test_endpoints_are_github_only(self):
        self.assertEqual(
            auth_config.DEVICE_CODE_URL, "https://github.com/login/device/code"
        )
        self.assertEqual(
            auth_config.DEVICE_TOKEN_URL,
            "https://github.com/login/oauth/access_token",
        )
        self.assertEqual(
            auth_config.DEVICE_VERIFY_URL, "https://github.com/login/device"
        )
        self.assertEqual(auth_config.USER_URL, "https://api.github.com/user")
        self.assertEqual(
            set(auth_config.ALLOWED_HOSTS), {"github.com", "api.github.com"}
        )

    def test_scope_is_read_user_only(self):
        self.assertEqual(auth_config.SCOPE, "read:user")
        self.assertNotIn("repo", auth_config.SCOPE.split())


class OverrideTest(unittest.TestCase):
    def test_environment_overrides_the_builtin_id(self):
        providers = auth_config.load_providers(
            {"DISK_WARDEN_GITHUB_CLIENT_ID": "  Iv1.test  "}
        )
        github = providers["github"]
        self.assertEqual(github.client_id, "Iv1.test")
        self.assertEqual(github.client_id_source, "env")

    def test_blank_override_keeps_the_builtin_id(self):
        providers = auth_config.load_providers({"DISK_WARDEN_GITHUB_CLIENT_ID": "   "})
        self.assertEqual(
            providers["github"].client_id, auth_config.DEFAULT_GITHUB_CLIENT_ID
        )

    def test_overlong_override_is_ignored(self):
        providers = auth_config.load_providers(
            {"DISK_WARDEN_GITHUB_CLIENT_ID": "x" * 65}
        )
        self.assertEqual(
            providers["github"].client_id, auth_config.DEFAULT_GITHUB_CLIENT_ID
        )

    def test_loader_accepts_an_environ_mapping(self):
        providers = auth_config.load_providers(None)
        self.assertIn("github", providers)


class StarAndRepoTest(unittest.TestCase):
    def test_star_url_uses_the_public_endpoint(self):
        url = auth_config.star_url("octo cat")
        self.assertEqual(
            url,
            "https://api.github.com/users/octo%20cat/starred/QuotaFuse-ai/disk-warden",
        )
        self.assertNotIn("token", url)

    def test_repository_url_is_public(self):
        self.assertEqual(
            auth_config.REPO_WEB_URL,
            "https://github.com/QuotaFuse-ai/disk-warden",
        )

    def test_gitee_is_a_guide_only_entry(self):
        gitee = auth_config.gitee_repository()
        self.assertEqual(gitee["provider"], "gitee")
        self.assertEqual(
            gitee["url"], "https://gitee.com/QuotaFuse-ai/disk-warden"
        )
        # The mirror does not exist yet: the UI shows "coming soon".
        self.assertFalse(gitee["available"])
        self.assertNotIn("client_id", gitee)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
