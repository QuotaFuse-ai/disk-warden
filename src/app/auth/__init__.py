# -*- coding: utf-8 -*-
"""Optional GitHub sign-in and Star guidance (T16).

Module map:

* :mod:`.config` -- the public OAuth app id, endpoints, scope and the Gitee
  repository guide (no secret exists in this flow);
* :mod:`.device_flow` -- RFC 8628 state machine: ``user_code`` + polling;
* :mod:`.client` -- device-code request, token poll, profile and public star
  check, all restricted to ``github.com`` / ``api.github.com``;
* :mod:`.store` -- ``config/auth.json`` state and the DPAPI token blob;
* :mod:`.manager` -- the single object :class:`src.app.service.AppService`
  talks to.

Signing in is optional: every local feature works when the user skips it.
Python 3.8 compatible (decision 0005).
"""

from .config import (
    GITHUB,
    GITEE_REPO_AVAILABLE,
    GITEE_REPO_URL,
    PROVIDER_IDS,
    REPO_WEB_URL,
    OAuthProviderConfig,
    gitee_repository,
    load_providers,
)
from .manager import AuthManager

__all__ = [
    "AuthManager",
    "GITEE_REPO_AVAILABLE",
    "GITEE_REPO_URL",
    "GITHUB",
    "OAuthProviderConfig",
    "PROVIDER_IDS",
    "REPO_WEB_URL",
    "gitee_repository",
    "load_providers",
]
