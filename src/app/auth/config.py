# -*- coding: utf-8 -*-
"""GitHub sign-in configuration for the welcome page (T16).

Sign-in uses the **OAuth 2.0 Device Authorization Grant** (RFC 8628), so the
host never opens a local callback port, never needs a client secret, and never
has to hold a loopback listener open while the user is in the browser.

A GitHub OAuth app client id is a *public* identifier -- it is not a secret and
ships inside every open-source desktop client.  Issue QUOT-18 (2026-10-21)
therefore pins the project's own id as a constant and allows an environment
override for testing:

=====================================  ====================================
``DEFAULT_GITHUB_CLIENT_ID``           public id of the Disk Warden OAuth app
``DISK_WARDEN_GITHUB_CLIENT_ID``       optional override (tests, forks)
=====================================  ====================================

There is deliberately **no** ``client_secret`` anywhere in this module: device
flow does not use one, and the app has never been issued one.  Gitee is not a
login provider (no device flow; its token exchange would need a bundled
secret) -- the UI only links to the Gitee repository page, see
:func:`gitee_repository`.

Python 3.8 compatible (decision 0005).
"""

import os
from dataclasses import dataclass
from urllib.parse import quote

GITHUB = "github"
PROVIDER_IDS = (GITHUB,)

#: Public OAuth app identifier (owner ``@QuotaFuse-ai``, device flow enabled).
DEFAULT_GITHUB_CLIENT_ID = "Ov23liZtqNoNtCAt8X8h"
ENV_GITHUB_CLIENT_ID = "DISK_WARDEN_GITHUB_CLIENT_ID"

#: Only these hosts may be contacted (issue QUOT-18, second-stage rules).
DEVICE_CODE_URL = "https://github.com/login/device/code"
DEVICE_TOKEN_URL = "https://github.com/login/oauth/access_token"
DEVICE_VERIFY_URL = "https://github.com/login/device"
USER_URL = "https://api.github.com/user"
ALLOWED_HOSTS = ("github.com", "api.github.com")

#: ``read:user`` is enough: profile plus the public starred endpoint.
SCOPE = "read:user"

FLOW_DEVICE_CODE = "device_code"

REASON_MISSING_CLIENT_ID = "missing_client_id"

REPO_OWNER = "QuotaFuse-ai"
REPO_NAME = "disk-warden"
REPO_WEB_URL = "https://github.com/%s/%s" % (REPO_OWNER, REPO_NAME)

#: The Gitee mirror does not exist yet; the button is rendered but disabled
#: with a "coming soon" label until this flag flips (issue QUOT-18).
GITEE_REPO_AVAILABLE = False
GITEE_REPO_URL = "https://gitee.com/%s/%s" % (REPO_OWNER, REPO_NAME)


def star_url(login):
    # type: (str) -> str
    """Public star state of the project repository for ``login``.

    Returns 204 when starred and 404 when not, and needs no scope beyond the
    token the device flow already yields -- which is why T16 does not ask for
    ``repo`` / ``public_repo``.
    """
    return "https://api.github.com/users/%s/starred/%s/%s" % (
        quote(str(login or ""), safe=""),
        REPO_OWNER,
        REPO_NAME,
    )


@dataclass(frozen=True)
class OAuthProviderConfig:
    """Immutable snapshot of the (single) sign-in provider."""

    provider: str = ""
    label: str = ""
    client_id: str = ""
    client_id_env: str = ""
    client_id_source: str = "builtin"
    device_code_url: str = DEVICE_CODE_URL
    token_url: str = DEVICE_TOKEN_URL
    verify_url: str = DEVICE_VERIFY_URL
    user_url: str = USER_URL
    scope: str = SCOPE

    @property
    def configured(self):
        # type: () -> bool
        return bool(self.client_id)

    @property
    def disabled_reason(self):
        # type: () -> str
        """Empty when the button may be enabled, else a stable reason code."""
        return "" if self.configured else REASON_MISSING_CLIENT_ID

    def public_dict(self):
        # type: () -> dict
        """Safe-to-send view: the client id itself is never sent."""
        return {
            "provider": self.provider,
            "label": self.label,
            "configured": self.configured,
            "disabled_reason": self.disabled_reason,
            "client_id_env": self.client_id_env,
            "flow": FLOW_DEVICE_CODE,
            "device_flow": True,
            "scopes": [part for part in self.scope.split() if part],
        }


def _text(raw):
    # type: (object) -> str
    if not isinstance(raw, str):
        return ""
    return raw.strip()


def load_providers(environ=None):
    # type: (object) -> dict
    """Build ``provider id -> OAuthProviderConfig``.

    The environment variable overrides the built-in id; an override is only
    accepted when it looks like a plausible id (non-empty, ASCII, <= 64
    characters) so a typo cannot silently disable sign-in.
    """
    env = environ if environ is not None else os.environ
    override = _text(env.get(ENV_GITHUB_CLIENT_ID))
    source = "env" if override and len(override) <= 64 else "builtin"
    client_id = override if source == "env" else DEFAULT_GITHUB_CLIENT_ID
    return {
        GITHUB: OAuthProviderConfig(
            provider=GITHUB,
            label="GitHub",
            client_id=client_id,
            client_id_env=ENV_GITHUB_CLIENT_ID,
            client_id_source=source,
        )
    }


def gitee_repository():
    # type: () -> dict
    """Guide-only entry for the Gitee star button (no authorization at all)."""
    return {
        "provider": "gitee",
        "label": "Gitee",
        "url": GITEE_REPO_URL,
        "available": GITEE_REPO_AVAILABLE,
    }


__all__ = [
    "ALLOWED_HOSTS",
    "DEFAULT_GITHUB_CLIENT_ID",
    "DEVICE_CODE_URL",
    "DEVICE_TOKEN_URL",
    "DEVICE_VERIFY_URL",
    "ENV_GITHUB_CLIENT_ID",
    "FLOW_DEVICE_CODE",
    "GITEE_REPO_AVAILABLE",
    "GITEE_REPO_URL",
    "GITHUB",
    "OAuthProviderConfig",
    "PROVIDER_IDS",
    "REASON_MISSING_CLIENT_ID",
    "REPO_NAME",
    "REPO_OWNER",
    "REPO_WEB_URL",
    "SCOPE",
    "USER_URL",
    "gitee_repository",
    "load_providers",
    "star_url",
]
