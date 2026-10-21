# -*- coding: utf-8 -*-
"""HTTP client for GitHub device-flow sign-in (T16).

Four user-initiated calls, all against GitHub only:

* ``POST /login/device/code``  -- start a device authorization;
* ``POST /login/oauth/access_token`` -- poll for the token (RFC 8628);
* ``GET  /user``               -- read the signed-in profile;
* ``GET  /users/<login>/starred/QuotaFuse-ai/disk-warden`` -- public star check.

The transport is :mod:`urllib.request` from the standard library, so the
installer gains no dependency.  ``opener`` is injectable so tests never touch
the network.  Every URL is checked against :data:`config.ALLOWED_HOSTS` --
a compromised caller cannot aim the token at another host.

No client secret exists in this flow; the token is only ever placed in an
``Authorization`` header and is never logged.

Python 3.8 compatible (decision 0005).
"""

import json
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from .. import APP_VERSION
from ..errors import BridgeError

from .config import ALLOWED_HOSTS, star_url

DEFAULT_TIMEOUT_SECONDS = 15.0
MAX_RESPONSE_BYTES = 256 * 1024
USER_AGENT = "DiskWarden/%s" % APP_VERSION

HEADER_ACCEPT_JSON = "application/json"
HEADER_FORM = "application/x-www-form-urlencoded"

DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"

#: Device-flow error codes that end the flow with a readable reason.
DEVICE_FATAL_ERRORS = ("expired_token", "access_denied", "device_flow_disabled")


def _decode_json(body):
    # type: (bytes) -> dict
    if not body:
        return {}
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _decode_payload(body):
    # type: (bytes) -> dict
    """GitHub answers JSON when asked, form-encoded otherwise."""
    payload = _decode_json(body)
    if payload:
        return payload
    parsed = parse_qs(body.decode("utf-8", "replace"))
    return {key: values[0] for key, values in parsed.items() if values}


def _int_in(value, default, minimum):
    # type: (object, int, int) -> int
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return number if number >= minimum else default


class OAuthClient(object):
    """GitHub-scoped HTTP calls; never logs device codes or tokens."""

    def __init__(self, provider, timeout_seconds=DEFAULT_TIMEOUT_SECONDS, opener=None):
        # type: (object, float, object) -> None
        self.provider = provider
        self.timeout_seconds = float(timeout_seconds)
        self._opener = opener or urlopen

    # -- device flow ------------------------------------------------------

    def request_device_code(self):
        # type: () -> dict
        """Ask GitHub for a ``user_code`` the human types in the browser."""
        data = urlencode(
            {"client_id": self.provider.client_id, "scope": self.provider.scope}
        ).encode("ascii")
        status, body = self._send(
            "POST",
            self.provider.device_code_url,
            data=data,
            headers={"Content-Type": HEADER_FORM},
        )
        payload = _decode_payload(body)
        error = str(payload.get("error") or "")
        if status < 200 or status >= 300 or error:
            raise BridgeError(
                "AUTH_FAILED",
                "GitHub refused the device-code request",
                {
                    "provider": self.provider.provider,
                    "stage": "device_code",
                    "status": status,
                    "reason": error or "device_code_rejected",
                },
            )
        device_code = payload.get("device_code")
        user_code = payload.get("user_code")
        verify = payload.get("verification_uri") or payload.get("verification_url")
        if not (
            isinstance(device_code, str)
            and device_code
            and isinstance(user_code, str)
            and user_code
            and isinstance(verify, str)
            and verify
        ):
            raise BridgeError(
                "AUTH_FAILED",
                "the device-code response is incomplete",
                {"provider": self.provider.provider, "stage": "device_code"},
            )
        return {
            "device_code": device_code,
            "user_code": user_code,
            # GitHub returns ``verification_uri``; keep the RFC 8628 spelling
            # and the legacy ``verification_url`` as a fallback.
            "verification_uri": verify,
            "expires_in": _int_in(payload.get("expires_in"), 900, 30),
            "interval": _int_in(payload.get("interval"), 5, 1),
        }

    def poll_device_token(self, device_code):
        # type: (str) -> dict
        """One poll round.

        Returns the raw GitHub payload.  ``access_token`` means success;
        ``error`` is one of ``authorization_pending`` / ``slow_down`` /
        ``expired_token`` / ``access_denied`` / ``device_flow_disabled``.
        """
        data = urlencode(
            {
                "client_id": self.provider.client_id,
                "device_code": device_code,
                "grant_type": DEVICE_GRANT_TYPE,
            }
        ).encode("ascii")
        status, body = self._send(
            "POST",
            self.provider.token_url,
            data=data,
            headers={"Content-Type": HEADER_FORM},
        )
        payload = _decode_payload(body)
        if status >= 500 or status == 429:
            raise BridgeError(
                "AUTH_FAILED",
                "GitHub is temporarily unavailable",
                {
                    "provider": self.provider.provider,
                    "stage": "device_poll",
                    "status": status,
                    "reason": "network",
                },
            )
        if not payload:
            raise BridgeError(
                "AUTH_FAILED",
                "the token endpoint returned an empty response",
                {
                    "provider": self.provider.provider,
                    "stage": "device_poll",
                    "status": status,
                    "reason": "network",
                },
            )
        token = payload.get("access_token")
        if isinstance(token, str) and token:
            return payload
        # GitHub answers 200 for ``authorization_pending`` / ``slow_down`` /
        # the terminal errors; a non-2xx or an unlabelled body is fatal.
        if status < 200 or status >= 300 or not payload.get("error"):
            raise BridgeError(
                "AUTH_FAILED",
                "the token endpoint rejected the poll",
                {
                    "provider": self.provider.provider,
                    "stage": "device_poll",
                    "status": status,
                    "reason": str(payload.get("error") or "token_rejected")[:80],
                },
            )
        return payload

    # -- profile ----------------------------------------------------------

    def fetch_user(self, token):
        # type: (str) -> dict
        status, body = self._send("GET", self.provider.user_url, token=token)
        payload = _decode_json(body)
        if status == 401 or status == 403:
            raise BridgeError(
                "AUTH_REQUIRED", "the token was rejected", {"status": status}
            )
        if status < 200 or status >= 300:
            raise BridgeError(
                "AUTH_FAILED",
                "could not read the profile",
                {"provider": self.provider.provider, "stage": "profile", "status": status},
            )
        login = payload.get("login") or ""
        if not isinstance(login, str) or not login:
            raise BridgeError(
                "AUTH_FAILED",
                "the profile has no login name",
                {"provider": self.provider.provider, "stage": "profile"},
            )
        avatar = payload.get("avatar_url")
        return {
            "provider": self.provider.provider,
            "login": login,
            "name": str(payload.get("name") or "")[:120],
            "avatar_url": avatar if isinstance(avatar, str) else "",
        }

    # -- star guidance ----------------------------------------------------

    def check_starred(self, token, login):
        # type: (str, str) -> bool
        """204 = starred, 404 = not starred; needs no extra scope."""
        status, _body = self._send("GET", star_url(login), token=token)
        if status == 204:
            return True
        if status == 404:
            return False
        if status in (401, 403):
            raise BridgeError("AUTH_REQUIRED", "the token was rejected", {"status": status})
        raise BridgeError(
            "AUTH_FAILED",
            "could not read the star state",
            {"provider": self.provider.provider, "stage": "star_status", "status": status},
        )

    # -- transport --------------------------------------------------------

    def _send(self, method, url, token="", data=None, headers=None):
        # type: (str, str, str, object, object) -> tuple
        self._ensure_allowed(url)
        request_headers = {"Accept": HEADER_ACCEPT_JSON, "User-Agent": USER_AGENT}
        request_headers.update(headers or {})
        if token:
            request_headers["Authorization"] = "Bearer %s" % token
        request = Request(url, data=data, headers=request_headers, method=method)
        try:
            response = self._opener(request, timeout=self.timeout_seconds)
        except HTTPError as exc:
            body = exc.read(MAX_RESPONSE_BYTES) if hasattr(exc, "read") else b""
            close = getattr(exc, "close", None)
            if callable(close):
                close()
            return int(getattr(exc, "code", 0) or 0), body
        except (URLError, OSError, ValueError) as exc:
            raise BridgeError(
                "AUTH_FAILED",
                "network error: %s" % exc,
                {
                    "provider": self.provider.provider,
                    "stage": method.lower(),
                    "reason": "network",
                },
            )
        try:
            status = getattr(response, "status", None)
            if status is None:
                getter = getattr(response, "getcode", None)
                status = getter() if callable(getter) else 0
            body = response.read(MAX_RESPONSE_BYTES)
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        return int(status or 0), body

    @staticmethod
    def _ensure_allowed(url):
        # type: (str) -> None
        host = (urlparse(url).hostname or "").lower()
        if host not in ALLOWED_HOSTS:
            raise BridgeError(
                "AUTH_FAILED",
                "refusing to contact a host outside the allow-list",
                {"stage": "transport", "reason": "blocked_host", "host": host[:80]},
            )


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "DEVICE_FATAL_ERRORS",
    "DEVICE_GRANT_TYPE",
    "MAX_RESPONSE_BYTES",
    "OAuthClient",
    "USER_AGENT",
]
