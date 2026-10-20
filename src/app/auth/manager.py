# -*- coding: utf-8 -*-
"""Sign-in orchestration for the welcome page and the Star prompt (T16).

The manager owns exactly one device-flow attempt at a time.  Responsibilities
are split so each piece stays small:

* :mod:`.config` holds the public client id, endpoints and scope;
* :mod:`.client` performs the four GitHub calls;
* :mod:`.device_flow` owns the ``user_code`` / polling state machine;
* :mod:`.store` persists non-secret state and a DPAPI-protected token.

Rules kept here, not in the UI:

* skipping the login is always possible -- no method here is required for the
  local features to work;
* the token never leaves the process except in an ``Authorization`` header,
  and it never appears in :meth:`status`;
* the Star button only opens the repository page and re-reads the *public*
  star state, so the token never needs ``public_repo``;
* closing the app cancels the flow and stops the poll thread.

Python 3.8 compatible (decision 0005).
"""

import logging
import threading
from datetime import datetime, timezone

from ..errors import BridgeError, invalid

from .client import OAuthClient
from .config import REPO_WEB_URL, load_providers
from .device_flow import ACTIVE_STATES, DeviceFlow
from .store import AuthStateStore, TokenStore

LOGGER = logging.getLogger("disk_warden.app.auth.manager")

STATE_AUTHENTICATED = "authenticated"


def _utc_now():
    # type: () -> str
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


class AuthManager(object):
    """Provider map + current flow + persisted welcome/Star state."""

    def __init__(
        self,
        config_dir,
        environ=None,
        opener=None,
        http_opener=None,
        provider_loader=None,
    ):
        # type: (str, object, object, object, object) -> None
        self.environ = dict(environ if environ is not None else {})
        self._provider_loader = provider_loader or load_providers
        self.providers = self._provider_loader(self.environ)
        self._state_store = AuthStateStore(config_dir)
        self._token_store = TokenStore(config_dir)
        self._state = self._state_store.load()
        self._lock = threading.RLock()
        self._flow = None
        self._opener = opener
        self._http_opener = http_opener

    # -- read-only -------------------------------------------------------

    def providers_public(self):
        # type: () -> list
        """Provider card for the welcome page; no client id."""
        return [
            provider.public_dict() for provider in self.providers.values()
        ]

    def status(self):
        # type: () -> dict
        with self._lock:
            state = dict(self._state)
            flow = self._flow.status() if self._flow is not None else None
            login = state.get("login")
            return {
                "providers": self.providers_public(),
                "signed_in": isinstance(login, dict),
                "login": dict(login) if isinstance(login, dict) else None,
                "welcome_seen": bool(state.get("welcome_seen")),
                "star": {
                    "pending": bool(state.get("star_prompt_pending")),
                    "dismissed": bool(state.get("star_prompt_dismissed")),
                    "starred": bool((login or {}).get("starred", False)),
                },
                "secure_storage": self._token_store.available(),
                "flow": flow,
            }

    # -- welcome page ----------------------------------------------------

    def mark_welcome_seen(self):
        # type: () -> dict
        with self._lock:
            state = dict(self._state)
            state["welcome_seen"] = True
            self._state = self._state_store.save(state)
        return self.status()

    # -- login flow ------------------------------------------------------

    def start(self, provider_id):
        # type: (object) -> dict
        """Request a device code and start polling; returns immediately."""
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise invalid("provider is required")
        provider_id = provider_id.strip().casefold()
        provider = self.providers.get(provider_id)
        if provider is None:
            raise invalid("unknown provider", provider=provider_id)
        if not provider.configured:
            # Keep the greyed-out button honest even if the built-in id is
            # ever removed from the build.
            raise BridgeError(
                "AUTH_NOT_CONFIGURED",
                "sign-in is not configured on this host",
                {"provider": provider.provider, "env": provider.client_id_env},
            )

        with self._lock:
            previous = self._flow
            self._flow = None
        if previous is not None:
            previous.close()

        client = OAuthClient(provider, opener=self._http_opener)
        try:
            device = client.request_device_code()
        except BridgeError as exc:
            detail = dict(exc.detail)
            detail.setdefault("provider", provider.provider)
            raise BridgeError(exc.code, exc.reason, detail)

        flow = DeviceFlow(provider, device, client, opener=self._opener)
        flow.set_completion(
            lambda token, payload: self._complete(flow, client, token, payload)
        )
        with self._lock:
            self._flow = flow
        flow.start()
        return self.status()

    def cancel(self):
        # type: () -> dict
        with self._lock:
            flow = self._flow
        if flow is not None:
            flow.cancel()
        return self.status()

    def sign_out(self):
        # type: () -> dict
        self.cancel()
        self._token_store.clear()
        with self._lock:
            state = dict(self._state)
            state["login"] = None
            state["star_prompt_pending"] = False
            state["star_prompt_dismissed"] = False
            self._state = self._state_store.save(state)
            self._flow = None
        return self.status()

    def _complete(self, flow, client, token, payload):
        # type: (object, OAuthClient, str, dict) -> None
        """Runs on the poll thread once GitHub returns a token."""
        try:
            user = client.fetch_user(token)
        except BridgeError as exc:
            flow.mark_failed(exc.code, exc.reason or "profile", exc.detail)
            return

        starred = None
        try:
            starred = client.check_starred(token, user["login"])
        except BridgeError as exc:
            # Star state is advisory: a signed-in user is still signed in.
            LOGGER.info("star state unavailable: %s", exc.reason)

        remembered = self._token_store.save(
            {
                "access_token": token,
                "token_type": str(payload.get("token_type") or "bearer"),
                "provider": flow.provider.provider,
                "scope": str(payload.get("scope") or "")[:200],
                "saved_at": _utc_now(),
            }
        )
        login = {
            "provider": flow.provider.provider,
            "login": user["login"],
            "name": user.get("name", ""),
            "avatar_url": user.get("avatar_url", ""),
            "starred": bool(starred),
            "authenticated_at": _utc_now(),
        }
        with self._lock:
            state = dict(self._state)
            state["login"] = login
            # A fresh sign-in re-arms the Star prompt unless the repo is
            # already starred; an old dismissal never hides a new login.
            state["star_prompt_dismissed"] = False
            state["star_prompt_pending"] = not bool(starred)
            self._state = self._state_store.save(state)
        if not remembered:
            LOGGER.warning(
                "the token is kept in memory only (secure storage unavailable)"
            )
        flow.mark_authenticated(login, starred)

    # -- star guidance ---------------------------------------------------

    def show_star(self):
        # type: () -> dict
        """Re-open the Star overlay from Settings (requirement: closable)."""
        with self._lock:
            state = dict(self._state)
            state["star_prompt_pending"] = True
            state["star_prompt_dismissed"] = False
            self._state = self._state_store.save(state)
        return self.status()

    def dismiss_star(self):
        # type: () -> dict
        with self._lock:
            state = dict(self._state)
            state["star_prompt_pending"] = False
            state["star_prompt_dismissed"] = True
            self._state = self._state_store.save(state)
        return self.status()

    def star(self):
        # type: () -> dict
        """Open the repository page, then re-read the public star state.

        Starring itself happens on GitHub -- the token only has ``read:user``,
        so writing a star is not attempted and no broader scope is requested.
        """
        self._open_repo()
        return self.check_star()

    def check_star(self):
        # type: () -> dict
        """Re-read the public star state and update the overlay."""
        with self._lock:
            login = dict(self._state.get("login") or {})
            provider_id = login.get("provider")
        provider = self.providers.get(provider_id) if isinstance(provider_id, str) else None
        if provider is None or not login.get("login"):
            raise BridgeError(
                "AUTH_REQUIRED", "sign in first to star the repository"
            )

        token = self._access_token()
        if not token:
            raise BridgeError(
                "AUTH_REQUIRED",
                "the stored token is not available in this session",
                {"provider": provider.provider},
            )

        client = OAuthClient(provider, opener=self._http_opener)
        starred = client.check_starred(token, login["login"])
        with self._lock:
            state = dict(self._state)
            updated = dict(state.get("login") or {})
            updated["starred"] = bool(starred)
            if starred:
                updated["starred_at"] = _utc_now()
            state["login"] = updated
            if starred:
                state["star_prompt_pending"] = False
            self._state = self._state_store.save(state)
        return self.status()

    def _open_repo(self):
        # type: () -> None
        opener = self._opener
        if callable(opener):
            try:
                opener(REPO_WEB_URL)
            except Exception as exc:  # a missing browser must not break the UI
                LOGGER.info("could not open the repository page: %s", exc)

    def _access_token(self):
        # type: () -> str
        payload = self._token_store.load() or {}
        token = payload.get("access_token")
        return token if isinstance(token, str) else ""

    # -- lifecycle -------------------------------------------------------

    def close(self):
        # type: () -> None
        with self._lock:
            flow = self._flow
            self._flow = None
        if flow is not None:
            try:
                flow.close()
            except Exception as exc:  # closing must never raise
                LOGGER.warning("could not close the sign-in flow: %s", exc)


__all__ = ["ACTIVE_STATES", "AuthManager", "STATE_AUTHENTICATED"]
