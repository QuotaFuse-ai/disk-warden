# -*- coding: utf-8 -*-
"""Application service: the one place where UI intents meet the engines.

The bridge (``bridge.py``) is a thin JSON-RPC adapter over this class.  The
service owns:

* the local stores (settings, journal, history, learned rules);
* one scan snapshot, so item ids stay stable between preview and migrate;
* planning and single-use confirm tokens;
* the background job runner and the event bus;
* startup recovery inspection.

Safety rules enforced here (they are not left to the UI):

* the UI submits item ids, never filesystem paths;
* red items and reparse roots can never enter a plan;
* running/unknown processes block a plan before anything is touched;
* duplicate final paths inside one plan are rejected instead of overwriting;
* the migration journal and migration history - which the engines do not
  write - are written here, around the engine calls;
* rollback writes its own journal/history/learned rule through the engine,
  so this layer never duplicates them.

Python 3.8 compatible (decision 0005).
"""

import logging
import math
import os
import shutil
import threading
import time
import traceback
from collections import OrderedDict
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit

from ..core.ai import (
    AIResolver,
    ENV_API_KEY,
    OpenAICompatibleProvider,
    ProviderError,
    keystore,
    load_config,
)
from ..core.migrate import (
    ERROR_CANCELLED,
    STATE_DONE,
    STATE_FAILED,
    STATE_LINKED_CLEANUP_PENDING,
    STATE_RECOVERY_REQUIRED,
    STATE_ROLLED_BACK,
    MigrationCancelled,
    MigrationRequest,
    complete_migration,
    prepare_copy,
    recover_job,
)
from ..core.migrate import paths as migrate_paths
from ..core.rollback import (
    RollbackRequest,
    precheck_rollback,
    recover_rollback,
    rollback,
)
from ..core.rules import (
    SAFETY_GREEN,
    SAFETY_RED,
    SAFETY_YELLOW,
    item_id_for,
    load_engine,
)
from ..core.scan import (
    STATUS_PRESENT,
    detect_processes,
    discover,
    load_catalog,
    measure,
    targets_from_discovery,
)
from ..core.store import (
    DataLayout,
    HistoryStore,
    JournalStore,
    LearnedRulesStore,
    RESULT_CANCELLED,
    RESULT_FAILURE,
    RESULT_ROLLED_BACK,
    RESULT_SUCCESS,
    SettingsStore,
    TYPE_MIGRATION,
    TYPE_ROLLBACK,
    atomic_write_json,
    can_change_root,
    default_data_root,
    utc_now,
)
from ..core.store.journal import (
    STATE_ALREADY_ROLLED_BACK,
    STATE_LINKED,
    STATE_PLANNED,
    STATE_RESIDUAL_COPY,
    STATE_ROLLBACK_CONFIG_RESTORED,
    STATE_ROLLBACK_COPIED,
    STATE_ROLLBACK_COPYING,
    STATE_ROLLBACK_FAILED,
    STATE_ROLLBACK_LINK_REMOVED,
    STATE_ROLLBACK_PLANNED,
    STATE_ROLLBACK_RECOVERY_REQUIRED,
    STATE_ROLLBACK_SOURCE_READY,
    STATE_ROLLBACK_VERIFIED,
    STATE_SOURCE_RENAMED,
    STATE_STATE_ABNORMAL,
    STATE_TARGET_READY,
    STATE_VERIFIED_LINK,
)
from ..platform import (
    Autostart,
    AutostartError,
    expand_path,
    is_junction,
    open_url,
    reveal_directory,
    volume_info,
)
from . import APP_VERSION, PROTOCOL_VERSION
from .auth import AuthManager, REPO_WEB_URL, gitee_repository
from .errors import BridgeError, invalid, not_found
from .events import EventBus
from .jobs import RECOVER, ROLLBACK, MIGRATE, SCAN, STATE_CANCELLED, JobRunner
from .plans import PlanRegistry

LOGGER = logging.getLogger("disk_warden.app")

APP_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(APP_DIR))
DEFAULT_CATALOG_PATH = os.path.join(REPO_ROOT, "data", "ide-catalog.json")
DEFAULT_RULES_PATH = os.path.join(REPO_ROOT, "data", "rules.json")

SPACE_MARGIN_BYTES = 64 * 1024 * 1024
SPACE_FACTOR = 1.05

MAX_EXTERNAL_URL_LENGTH = 2048

AI_TEST_TIMEOUT_SECONDS = 8.0
AI_TEST_MAX_TOKENS = 1
AI_KEY_MAX_LENGTH = keystore.MAX_SECRET_BYTES


def _validate_external_url(url):
    # type: (object) -> str
    """Only GitHub URLs under the QuotaFuse-ai organisation are allowed."""
    if not isinstance(url, str) or not url:
        raise invalid("url is required")
    if len(url) > MAX_EXTERNAL_URL_LENGTH:
        raise invalid("url is too long")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in url):
        raise invalid("url must not contain control characters")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise invalid("url is not valid")
    path = unquote(parsed.path)
    segments = path.split("/")
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or not path.startswith("/QuotaFuse-ai/")
        or "\\" in path
        or "." in segments
        or ".." in segments
    ):
        raise BridgeError(
            "FORBIDDEN",
            "url is not on the allowed list",
            {"url": url},
        )
    return url

MIGRATION_TERMINAL = frozenset(
    [
        STATE_DONE,
        STATE_FAILED,
        STATE_ROLLED_BACK,
        STATE_RECOVERY_REQUIRED,
        STATE_LINKED_CLEANUP_PENDING,
    ]
)

# Rollback journal states that still need convergence after a restart.
# ``ROLLBACK_FAILED`` is deliberately absent: the rollback engine treats it as
# a terminal outcome, so "recover" on such a record only re-reads the terminal
# state and reports success while the banner comes back on every refresh -- an
# entry the user could never clear.  The interrupted migration keeps its own
# non-terminal record and stays actionable.
ROLLBACK_UNFINISHED = frozenset(
    [
        STATE_ROLLBACK_PLANNED,
        STATE_ROLLBACK_LINK_REMOVED,
        STATE_ROLLBACK_COPYING,
        STATE_ROLLBACK_COPIED,
        STATE_ROLLBACK_VERIFIED,
        STATE_ROLLBACK_SOURCE_READY,
        STATE_ROLLBACK_CONFIG_RESTORED,
        STATE_ROLLBACK_RECOVERY_REQUIRED,
    ]
)

# States where no automatic convergence is left: the engine either has to make
# a judgement call it refuses to guess at, or the leftover is informational.
# The user may hide the banner without touching the data; the record stays on
# disk and the item comes back if the state moves again.
IGNORABLE_STATES = frozenset(
    [
        STATE_RECOVERY_REQUIRED,
        STATE_LINKED_CLEANUP_PENDING,
        STATE_ROLLBACK_RECOVERY_REQUIRED,
    ]
)

STATE_LABELS = {
    STATE_PLANNED: "已计划",
    "BACKED_UP": "配置已备份",
    "COPYING": "复制中",
    "COPIED": "复制完成",
    "COPY_VERIFIED": "副本已校验",
    STATE_TARGET_READY: "目标就绪",
    STATE_SOURCE_RENAMED: "源已改名",
    STATE_LINKED: "链接已建立",
    STATE_VERIFIED_LINK: "链接已校验",
    STATE_LINKED_CLEANUP_PENDING: "待清理副本",
    STATE_DONE: "已完成",
    STATE_FAILED: "失败",
    STATE_ROLLED_BACK: "已回滚",
    STATE_RECOVERY_REQUIRED: "需人工确认",
    STATE_ROLLBACK_PLANNED: "回滚已计划",
    STATE_ROLLBACK_LINK_REMOVED: "链接已移除",
    STATE_ROLLBACK_COPYING: "回滚复制中",
    STATE_ROLLBACK_COPIED: "回滚副本已复制",
    STATE_ROLLBACK_VERIFIED: "回滚副本已校验",
    STATE_ROLLBACK_SOURCE_READY: "源已恢复",
    STATE_ROLLBACK_CONFIG_RESTORED: "配置已恢复",
    STATE_ROLLBACK_FAILED: "回滚失败",
    STATE_ROLLBACK_RECOVERY_REQUIRED: "回滚需人工确认",
    STATE_ALREADY_ROLLED_BACK: "已回滚",
    STATE_RESIDUAL_COPY: "存在残留副本",
    STATE_STATE_ABNORMAL: "状态异常",
}


def _text(value):
    # type: (object) -> str
    return value.strip() if isinstance(value, str) else ""


def _normalize_source_path(path):
    # type: (object) -> str
    """Key used to match recorded migrations to scanned directories.

    Separator style and (on Windows) letter case must not matter, otherwise a
    restart would not recognise a directory that is already a junction.
    """
    if not isinstance(path, str):
        return ""
    value = path.strip().replace("\\", "/")
    if not value:
        return ""
    return os.path.normcase(os.path.normpath(value))


def _as_int(value, default=0):
    # type: (object, int) -> int
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


def _free_bytes(path):
    # type: (str) -> int
    try:
        return int(shutil.disk_usage(path).free)
    except OSError:
        return 0


def _volume_payload(path):
    # type: (str) -> dict
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return {"path": path, "available": False, "total": 0, "used": 0, "free": 0}
    payload = {
        "path": path,
        "available": True,
        "total": int(usage.total),
        "used": int(usage.used),
        "free": int(usage.free),
    }
    try:
        info = volume_info(path)
    except Exception:  # volume probing is advisory only
        info = None
    if info is not None:
        payload["mount"] = getattr(info, "mount_point", "") or ""
        payload["fs"] = getattr(info, "fs_name", "") or ""
        payload["is_ntfs"] = bool(getattr(info, "is_ntfs", False))
    return payload


def _seconds_between(start, end):
    # type: (object, object) -> object
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        first = datetime.fromisoformat(start.replace("Z", "+00:00"))
        second = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return None
    if first.tzinfo is None:
        first = first.replace(tzinfo=timezone.utc)
    if second.tzinfo is None:
        second = second.replace(tzinfo=timezone.utc)
    delta = (second - first).total_seconds()
    return int(delta) if delta >= 0 else None


def _need_bytes(total_bytes):
    # type: (int) -> int
    return int(math.ceil(max(0, total_bytes) * SPACE_FACTOR)) + SPACE_MARGIN_BYTES


def _elapsed_ms(started):
    # type: (float) -> int
    return int(round((time.monotonic() - started) * 1000))


def _fallback_root(environ):
    # type: (dict) -> str
    local = environ.get("LOCALAPPDATA") or ""
    if local:
        return os.path.join(local, "DiskWarden")
    return os.path.join(os.path.expanduser("~"), "DiskWarden")


class AppService(object):
    """Owns one data root and the engines that operate on it."""

    def __init__(
        self,
        data_root=None,
        catalog_path=None,
        rules_path=None,
        environ=None,
        process_provider=None,
        free_space_provider=None,
        transfer=None,
        ai_test_opener=None,
        autostart=None,
        auth_opener=None,
        auth_http_opener=None,
        auth_provider_loader=None,
    ):
        # type: (object, object, object, object, object, object, object, object, object, object, object, object) -> None
        self.environ = dict(environ if environ is not None else os.environ)
        self.catalog_path = os.path.abspath(
            catalog_path or DEFAULT_CATALOG_PATH
        )
        self.rules_path = os.path.abspath(rules_path or DEFAULT_RULES_PATH)
        self._process_provider = process_provider
        self._free_space_provider = free_space_provider
        self._transfer = transfer
        self._ai_test_opener = ai_test_opener
        self.autostart = autostart if autostart is not None else Autostart()

        self._stored_ai_key = ""
        self._ai_key_error = ""
        # Injectable only for tests: the browser opener never touches the
        # network in production, and the HTTP opener is replaced so the device
        # flow never reaches GitHub during a test run.
        self._auth_opener = auth_opener
        self._auth_http_opener = auth_http_opener
        self._auth_provider_loader = auth_provider_loader

        self.bus = EventBus()
        self.runner = JobRunner(self.bus)
        self.plans = PlanRegistry()

        self._lock = threading.RLock()
        self._snapshot = None
        self._settings = {}
        self._ai_config = None
        self._auth = None
        self._tool_names = {}
        self._started_at = utc_now()
        self._last_recovery = []

        root = data_root or self.environ.get("DISKWARDEN_DATA_ROOT") or default_data_root()
        try:
            self._bind(root)
        except OSError as exc:
            LOGGER.warning("data root %s is not usable (%s); falling back", root, exc)
            self._bind(_fallback_root(self.environ))
        self._last_recovery = self.recovery_list()["items"]

    # -- lifecycle -------------------------------------------------------

    def _bind(self, root):
        # type: (str) -> None
        """(Re)build the layout and stores for ``root``."""
        if not root:
            raise OSError("data root is empty")
        layout = DataLayout(os.path.abspath(os.path.normpath(root)))
        layout.ensure()
        if not layout.is_writable():
            raise OSError("data root is not writable: %s" % layout.root)
        self.layout = layout
        self.settings_store = SettingsStore(layout.settings_path)
        self.journals = JournalStore(layout.jobs_dir)
        self.history = HistoryStore(layout.history_path)
        self.learned = LearnedRulesStore(layout.learned_path)

        settings = self.settings_store.load(repair=True)
        if not settings.get("data_root"):
            settings["data_root"] = layout.root
            try:
                settings = self.settings_store.save(settings)
            except Exception as exc:  # defaults must not block startup
                LOGGER.warning("could not persist default settings: %s", exc)
        self._settings = settings
        self._refresh_ai()
        previous = self._auth
        if previous is not None:
            previous.close()
        self._auth = AuthManager(
            layout.config_dir,
            environ=self.environ,
            opener=self._auth_opener,
            http_opener=self._auth_http_opener,
            provider_loader=self._auth_provider_loader,
        )
        self._snapshot = None

    def close(self):
        # type: () -> None
        auth = self._auth
        if auth is not None:
            # Stops the device-flow poll thread so no request outlives the app.
            auth.close()
        self.runner.shutdown()

    # -- settings / AI ---------------------------------------------------

    def _ai_settings(self):
        # type: () -> dict
        return {
            "enabled": bool(self._settings.get("ai_enabled", False)),
            "base_url": _text(self._settings.get("ai_base_url")),
            "model": _text(self._settings.get("ai_model")),
            "api_key": self._stored_ai_key,
        }

    def _load_stored_ai_key(self):
        # type: () -> None
        """Cache the decrypted key; keep a stable error code when it fails.

        A key file copied from another account or machine cannot be decrypted
        by DPAPI.  That is a recoverable state, not a crash: the layer simply
        stays unavailable and the UI offers to clear the stored key.
        """
        try:
            self._stored_ai_key = keystore.load(self.layout.ai_key_path)
            self._ai_key_error = ""
        except keystore.KeystoreError as exc:
            self._stored_ai_key = ""
            self._ai_key_error = exc.code
            LOGGER.warning("stored AI key is unavailable (%s)", exc.code)

    def _ai_key_source(self):
        # type: () -> str
        if _text(self.environ.get(ENV_API_KEY)):
            return "env"
        if keystore.stored(self.layout.ai_key_path):
            return "stored"
        return "none"

    def _refresh_ai(self):
        # type: () -> None
        self._load_stored_ai_key()
        try:
            self._ai_config = load_config(
                self.environ,
                settings=self._ai_settings(),
                enabled=bool(
                    self._settings.get("ai_enabled", False)
                    and self._settings.get("ai_consent", False)
                ),
            )
        except Exception as exc:
            LOGGER.warning("AI config rejected: %s", exc)
            self._ai_config = load_config(self.environ, enabled=False)

    def _build_ai_resolver(self):
        # type: () -> object
        config = self._ai_config
        if config is None or not config.enabled or not config.available:
            return None
        try:
            provider = OpenAICompatibleProvider(
                config.base_url,
                config.model,
                config.api_key,
                timeout_seconds=config.timeout_seconds,
            )
            return AIResolver(provider, config, environ=self.environ)
        except Exception as exc:
            LOGGER.warning("AI resolver disabled: %s", exc)
            return None

    def _autostart_status(self):
        # type: () -> dict
        """Registry state of the current-user startup item (never raises)."""
        try:
            status = self.autostart.status()
        except Exception as exc:
            LOGGER.warning("could not read the startup item: %s", exc)
            return {
                "supported": False,
                "enabled": False,
                "command": "",
                "registered": "",
                "error": str(exc),
            }
        status = dict(status)
        status.setdefault("supported", False)
        status.setdefault("enabled", False)
        status.setdefault("command", "")
        status.setdefault("registered", "")
        status.setdefault("error", "")
        return status

    def _apply_autostart(self, enabled):
        # type: (bool) -> None
        """Write or remove the ``HKCU`` startup value, without elevation.

        A source checkout (and every non-Windows build) has no launch command
        to register, so the preference is only persisted there.  In a frozen
        build a registry failure is surfaced to the UI instead of leaving the
        switch looking enabled while nothing starts with Windows.
        """
        # Ask the object, not ``status()``: a registry read failure must not be
        # mistaken for "this build cannot start with Windows" and silently
        # drop the user's choice.
        try:
            supported = bool(self.autostart.supported)
        except Exception:
            supported = False
        if not supported:
            return
        try:
            self.autostart.set_enabled(enabled)
        except AutostartError as exc:
            raise BridgeError(
                "AUTOSTART_FAILED",
                "could not update the startup item: %s" % exc,
                {"code": getattr(exc, "code", "AUTOSTART_FAILED")},
            )
        except Exception as exc:
            raise BridgeError(
                "AUTOSTART_FAILED",
                "could not update the startup item: %s" % exc,
            )
        after = self._autostart_status()
        if bool(after["enabled"]) != bool(enabled):
            raise BridgeError(
                "AUTOSTART_FAILED",
                "the startup item did not change; check permissions for "
                "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run",
                {"enabled": bool(after["enabled"])},
            )

    def settings_get(self):
        # type: () -> dict
        with self._lock:
            settings = dict(self._settings)
        ai = self._ai_config
        public = ai.public_dict() if ai is not None else {}
        autostart = self._autostart_status()
        stored_startup = bool(settings.get("launch_at_startup", False))
        return {
            "app_version": APP_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "data_root": self.layout.root,
            "target_root": settings.get("data_root") or self.layout.root,
            "launch_at_startup": (
                autostart["enabled"] if autostart["supported"] else stored_startup
            ),
            "launch_at_startup_supported": autostart["supported"],
            "launch_at_startup_error": autostart["error"],
            "launch_at_startup_command": autostart["command"],
            "auto_maintenance": bool(settings.get("auto_maintenance", False)),
            "uninstall_policy": settings.get("uninstall_policy", "keep"),
            "language": settings.get("language", "en"),
            "ai": {
                "enabled": bool(public.get("enabled", False)),
                "available": bool(public.get("available", False)),
                "disabled_reason": public.get("disabled_reason", ""),
                "consent": bool(settings.get("ai_consent", False)),
                "base_url": public.get("base_url", ""),
                "model": public.get("model", ""),
                "api_key_set": bool(ai is not None and ai.api_key),
                "api_key_source": self._ai_key_source(),
                "api_key_error": self._ai_key_error,
                "warning_key": "ai.third_party_warning",
                "warning": "目录信息会发送给第三方 AI 服务",
            },
            "paths": self.layout.to_dict(),
            "settings_warnings": list(self.settings_store.warnings),
        }

    def settings_set(self, patch):
        # type: (object) -> dict
        if not isinstance(patch, dict):
            raise invalid("settings patch must be an object")

        with self._lock:
            settings = dict(self._settings)

        target_root = patch.get("target_root", patch.get("data_root"))
        if target_root is not None:
            if not isinstance(target_root, str) or not target_root.strip():
                raise invalid("target_root must be a non-empty path")
            proposed = os.path.abspath(os.path.normpath(os.path.expanduser(target_root)))
            if not can_change_root(self.layout.root, proposed):
                raise invalid(
                    "data root cannot change while migrated data exists",
                    current_root=self.layout.root,
                    proposed_root=proposed,
                )
            if os.path.normcase(proposed) != os.path.normcase(self.layout.root):
                if self.runner.has_active():
                    raise BridgeError("BUSY", "cannot change the data root while a job is running")
                previous = dict(settings)
                try:
                    self._bind(proposed)
                except OSError as exc:
                    raise invalid("target root is not writable: %s" % exc)
                previous["data_root"] = self.layout.root
                self._settings = previous
                self.settings_store.save(self._settings)
                self._refresh_ai()
                return self.settings_get()

        if "launch_at_startup" in patch:
            if not isinstance(patch["launch_at_startup"], bool):
                raise invalid("launch_at_startup must be a boolean")
            # Always reconcile the registry with the requested value: a stale
            # entry from an older extraction folder must not survive a toggle.
            self._apply_autostart(patch["launch_at_startup"])
            settings["launch_at_startup"] = patch["launch_at_startup"]
        if "auto_maintenance" in patch:
            if not isinstance(patch["auto_maintenance"], bool):
                raise invalid("auto_maintenance must be a boolean")
            settings["auto_maintenance"] = patch["auto_maintenance"]
        if "uninstall_policy" in patch:
            policy = patch["uninstall_policy"]
            if policy not in ("keep", "rollback"):
                raise invalid("uninstall_policy must be keep or rollback")
            settings["uninstall_policy"] = policy
        if "language" in patch:
            if not isinstance(patch["language"], str) or not patch["language"].strip():
                raise invalid("language must be a non-empty string")
            settings["language"] = patch["language"].strip()

        ai_patch = patch.get("ai")
        if ai_patch is not None:
            if not isinstance(ai_patch, dict):
                raise invalid("ai settings must be an object")
            for key, setting_key in (
                ("enabled", "ai_enabled"),
                ("consent", "ai_consent"),
            ):
                if key in ai_patch:
                    if not isinstance(ai_patch[key], bool):
                        raise invalid("%s.%s must be a boolean" % ("ai", key))
                    settings[setting_key] = ai_patch[key]
            for key, setting_key in (("base_url", "ai_base_url"), ("model", "ai_model")):
                if key in ai_patch:
                    if not isinstance(ai_patch[key], str):
                        raise invalid("ai.%s must be a string" % key)
                    settings[setting_key] = ai_patch[key].strip()

        if settings.get("ai_enabled") and not settings.get("ai_consent"):
            raise BridgeError(
                "AI_CONSENT_REQUIRED",
                "enabling AI requires accepting that directory information is sent "
                "to a third-party service",
                {"warning_key": "ai.third_party_warning"},
            )

        if ai_patch is not None:
            self._apply_ai_key(ai_patch)

        with self._lock:
            self._settings = settings
        self.settings_store.save(settings)
        self._refresh_ai()
        return self.settings_get()

    def _apply_ai_key(self, ai_patch):
        # type: (dict) -> bool
        """Encrypt or clear ``ai.api_key`` and return True when it changed.

        Only a DPAPI ciphertext reaches the disk.  The plaintext key is never
        part of a returned value, exception message or log record.
        """
        raw = None
        for name in ("api_key", "apiKey"):
            if name in ai_patch:
                if not isinstance(ai_patch[name], str):
                    raise invalid("ai.%s must be a string" % name)
                if raw is None:
                    raw = ai_patch[name]
        if raw is None:
            return False

        secret = raw.strip()
        path = self.layout.ai_key_path
        if not secret:
            try:
                keystore.clear(path)
            except keystore.KeystoreError as exc:
                LOGGER.warning("stored AI key could not be removed (%s)", exc.code)
                raise BridgeError(
                    "AI_KEY_STORE_FAILED", "the stored API key could not be removed"
                )
            return True

        if len(secret.encode("utf-8")) > AI_KEY_MAX_LENGTH:
            raise invalid("ai.api_key is too long")
        try:
            keystore.save(path, secret)
        except keystore.KeystoreError as exc:
            if exc.code == keystore.CODE_UNSUPPORTED:
                raise BridgeError(
                    "AI_KEY_UNSUPPORTED",
                    "local key storage is not supported on this platform",
                )
            LOGGER.warning("AI key could not be stored (%s)", exc.code)
            raise BridgeError("AI_KEY_STORE_FAILED", "the API key could not be stored")
        return True

    def ai_status(self):
        # type: () -> dict
        return self.settings_get()["ai"]

    def ai_consent(self):
        # type: () -> dict
        with self._lock:
            settings = dict(self._settings)
        settings["ai_consent"] = True
        self._settings = settings
        self.settings_store.save(settings)
        self._refresh_ai()
        return self.settings_get()["ai"]

    def ai_test(self, params=None):
        # type: (object) -> dict
        """Send one minimal chat request to verify the configured endpoint.

        ``params`` may carry unsaved ``base_url`` / ``model`` / ``api_key``
        values from the settings form; they are used for this one call and
        never written anywhere.  Failures are reported inline (``ok: False``)
        rather than raised, and the result never contains the key.
        """
        overrides = params if isinstance(params, dict) else {}
        for name in ("base_url", "model", "api_key"):
            if name in overrides and not isinstance(overrides[name], str):
                raise invalid("ai.%s must be a string" % name)

        config = self._ai_config
        base_url = _text(overrides.get("base_url")) or (
            config.base_url if config is not None else ""
        )
        model = _text(overrides.get("model")) or (
            config.model if config is not None else ""
        )
        api_key = _text(overrides.get("api_key")) or (
            config.api_key if config is not None else ""
        )

        def failure(code, message, latency_ms=0):
            return {
                "ok": False,
                "latency_ms": latency_ms,
                "model": model,
                "error_code": code,
                "message": message,
            }

        if not api_key:
            return failure("missing_api_key", "no API key is configured")
        if not base_url:
            return failure("missing_base_url", "no endpoint is configured")
        if not model:
            return failure("missing_model", "no model is configured")

        started = time.monotonic()
        try:
            provider = OpenAICompatibleProvider(
                base_url,
                model,
                api_key,
                timeout_seconds=AI_TEST_TIMEOUT_SECONDS,
                opener=self._ai_test_opener,
            )
            envelope = provider.classify(
                {
                    "model": model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": AI_TEST_MAX_TOKENS,
                    "stream": False,
                }
            )
        except ProviderError as exc:
            LOGGER.info("AI connection test failed (%s)", exc.code)
            return failure(exc.code, str(exc), _elapsed_ms(started))
        except Exception as exc:
            LOGGER.warning(
                "AI connection test failed (%s)", type(exc).__name__
            )
            return failure(
                "internal", "the connection test failed", _elapsed_ms(started)
            )

        reported = envelope.get("model")
        return {
            "ok": True,
            "latency_ms": _elapsed_ms(started),
            "model": reported if isinstance(reported, str) and reported else model,
            "error_code": "",
            "message": "",
        }

    # -- sign-in / welcome / star (T16) ----------------------------------

    def _auth_manager(self):
        # type: () -> object
        auth = self._auth
        if auth is None:  # pragma: no cover - _bind always builds one
            raise BridgeError("INTERNAL", "auth manager is not available")
        return auth

    def auth_status(self):
        # type: () -> dict
        return self._auth_payload(self._auth_manager().status())

    def _auth_payload(self, status):
        # type: (dict) -> dict
        # The repository URL is public and needed by the Star overlay; it is
        # not a secret and never contains an application id.  Every auth
        # method returns the same envelope so the UI can replace its state
        # from any of them.
        payload = dict(status)
        payload["repository_url"] = REPO_WEB_URL
        # Gitee is a link, not a provider: no token, no authorization code.
        payload["gitee"] = gitee_repository()
        return payload

    def auth_start(self, provider):
        # type: (object) -> dict
        return self._auth_payload(self._auth_manager().start(provider))

    def auth_cancel(self):
        # type: () -> dict
        return self._auth_payload(self._auth_manager().cancel())

    def auth_sign_out(self):
        # type: () -> dict
        return self._auth_payload(self._auth_manager().sign_out())

    def welcome_seen(self):
        # type: () -> dict
        return self._auth_payload(self._auth_manager().mark_welcome_seen())

    def star_show(self):
        # type: () -> dict
        return self._auth_payload(self._auth_manager().show_star())

    def star_dismiss(self):
        # type: () -> dict
        return self._auth_payload(self._auth_manager().dismiss_star())

    def star_give(self):
        # type: () -> dict
        """Open the repository page, then re-read the public star state."""
        return self._auth_payload(self._auth_manager().star())

    def star_check(self):
        # type: () -> dict
        """Re-read the public star state (no browser, no extra scope)."""
        return self._auth_payload(self._auth_manager().check_star())

    # -- read-only info --------------------------------------------------

    def hello(self):
        # type: () -> dict
        return {
            "app": "disk-warden",
            "version": APP_VERSION,
            "protocol": PROTOCOL_VERSION,
            "started_at": self._started_at,
            "settings": self.settings_get(),
            "auth": self.auth_status(),
            "disk": self.disk_info(),
            "scan": self._snapshot_summary(),
            "recovery": self.recovery_list(),
        }

    def disk_info(self):
        # type: () -> dict
        target = _volume_payload(self.layout.data_dir)
        source_root = self.environ.get("SystemDrive") or os.path.splitdrive(
            os.path.expanduser("~")
        )[0]
        source_root = (source_root or "C:") + os.sep
        if not os.path.isdir(source_root):
            source_root = os.path.expanduser("~")
        return {
            "target": target,
            "source": _volume_payload(source_root),
            "data_root": self.layout.root,
            "target_root": self.layout.data_dir,
            "target_is_ntfs": bool(target.get("is_ntfs", True)),
        }

    def events_poll(self, since_seq=0):
        # type: (object) -> dict
        return self.bus.poll(since_seq)

    # -- shell integration -----------------------------------------------

    def reveal(self, path):
        # type: (str) -> dict
        """Open ``path`` in the OS file manager (UI "open folder" buttons)."""
        try:
            target = reveal_directory(path)
        except FileNotFoundError:
            raise not_found("folder does not exist", path=expand_path(path))
        except OSError as exc:
            code = (
                "PERMISSION_DENIED"
                if getattr(exc, "winerror", None) == 5
                else "IO_ERROR"
            )
            raise BridgeError(
                code, "could not open folder: %s" % exc, {"path": path}
            )
        return {"opened": True, "path": target}

    def open_external(self, url):
        # type: (object) -> dict
        """Open a whitelisted web page in the default browser."""
        target = _validate_external_url(url)
        try:
            open_url(target)
        except OSError as exc:
            raise BridgeError(
                "IO_ERROR", "could not open browser: %s" % exc, {"url": target}
            )
        return {"opened": True, "url": target}

    def job_status(self, job_id):
        # type: (object) -> dict
        if not isinstance(job_id, str) or not job_id:
            raise invalid("job_id is required")
        try:
            return self.runner.status(job_id)
        except BridgeError:
            raise not_found("job not found", job_id=job_id)

    def job_cancel(self, job_id):
        # type: (object) -> dict
        if not isinstance(job_id, str) or not job_id:
            raise invalid("job_id is required")
        try:
            job = self.runner.get(job_id)
        except BridgeError:
            raise not_found("job not found", job_id=job_id)
        cancelled = self.runner.cancel(job_id)
        return {"job_id": job_id, "cancel_requested": cancelled, "state": job.state}

    def job_list(self, active_only=False):
        # type: (object) -> dict
        return {"jobs": self.runner.list(active_only=bool(active_only))}

    # -- scan ------------------------------------------------------------

    def scan_start(self):
        # type: () -> dict
        job_id = self.runner.start(SCAN, self._scan_worker, title="scan", check_busy=True)
        return {"job_id": job_id, "state": "queued"}

    def scan_cancel(self, job_id=None):
        # type: (object) -> dict
        job_id = job_id or self._latest_job_id(SCAN)
        if not job_id:
            raise not_found("no scan job to cancel")
        return self.job_cancel(job_id)

    def scan_status(self, job_id=None):
        # type: (object) -> dict
        if not job_id:
            job_id = self._latest_job_id(SCAN)
        if not job_id:
            return {"job": None, "recent_events": []}
        return self.job_status(job_id)

    def scan_result(self):
        # type: () -> dict
        with self._lock:
            snapshot = self._snapshot
        if snapshot is None:
            raise not_found("no scan result yet; run scan.start first")
        return snapshot

    def items_list(self, tool_id=None, safety=None):
        # type: (object, object) -> dict
        with self._lock:
            snapshot = self._snapshot
        if snapshot is None:
            raise not_found("no scan result yet; run scan.start first")
        items = snapshot["items"]
        if tool_id:
            items = [item for item in items if item["tool_id"] == tool_id]
        if safety:
            items = [item for item in items if item["safety"] == safety]
        return {"items": list(items), "stats": snapshot["stats"]}

    def _latest_job_id(self, kind):
        # type: (str) -> str
        matches = [job for job in self.runner.list() if job.get("kind") == kind]
        if not matches:
            return ""
        matches.sort(key=lambda job: job.get("created_at") or "", reverse=True)
        return matches[0].get("job_id") or ""

    def _snapshot_summary(self):
        # type: () -> dict
        with self._lock:
            snapshot = self._snapshot
        if snapshot is None:
            return {"available": False}
        return {
            "available": True,
            "scanned_at": snapshot.get("scanned_at"),
            "stats": snapshot.get("stats", {}),
        }

    def _scan_worker(self, job, token, emit):
        # type: (object, object, object) -> dict
        from ..core.scan import ScanCancelled

        catalog = load_catalog(self.catalog_path)
        emit(
            "scan.progress",
            {"step": "discover", "targets_total": 0, "targets_done": 0},
        )
        discovery = discover(catalog, environ=self.environ)
        if token.is_set():
            raise ScanCancelled("cancelled")

        targets = targets_from_discovery(discovery)
        emit(
            "job.progress",
            {
                "stage": "measure",
                "step": "measure",
                "targets_total": len(targets),
                "targets_done": 0,
            },
        )

        def progress(payload):
            data = dict(payload or {})
            data["stage"] = "measure"
            data["step"] = "measure"
            emit("job.progress", data, throttle=True)

        report = measure(
            targets,
            cancel_token=token,
            progress_cb=progress,
            throttle_seconds=0.25,
        )
        emit(
            "job.progress",
            {
                "stage": "measure",
                "step": "measure",
                "targets_total": len(targets),
                "targets_done": len(targets),
                "done": True,
            },
        )

        processes = detect_processes(catalog, process_provider=self._process_provider)
        engine = self._build_engine()
        tool_names = {tool.id: tool.name for tool in catalog.tools}
        self._tool_names = tool_names
        migrated = self._migrated_index()

        items = []
        for measured in report.items:
            item = self._build_item(
                measured,
                engine,
                processes,
                migrated.get(_normalize_source_path(measured.source_path)),
            )
            items.append(item)
            emit(
                "job.progress",
                {
                    "stage": "classify",
                    "step": "classify",
                    "item_id": item["item_id"],
                    "tool_id": item["tool_id"],
                },
                throttle=True,
            )

        tools = self._build_tool_rows(discovery, items, processes)
        warnings = [warning.to_dict() for warning in report.warnings]
        warnings.extend(
            {"code": "discovery", "message": str(warning)}
            for warning in getattr(discovery, "warnings", [])
            if not hasattr(warning, "to_dict")
        )
        for warning in getattr(discovery, "warnings", []):
            if hasattr(warning, "to_dict"):
                warnings.append(warning.to_dict())

        snapshot = {
            "scanned_at": utc_now(),
            "job_id": job.job_id,
            "data_root": self.layout.root,
            "target_root": self.layout.data_dir,
            "tools": tools,
            "items": items,
            "stats": self._stats(tools, items),
            "warnings": warnings,
            "processes": {key: value.to_dict() for key, value in processes.items()},
            "disk": self.disk_info(),
        }
        with self._lock:
            self._snapshot = snapshot
        emit("scan.done", {"step": "done", "stats": snapshot["stats"]})
        return {
            "items": len(items),
            "bytes": snapshot["stats"]["bytes"],
            "files": snapshot["stats"]["files"],
            "warnings": len(warnings),
        }

    def _migrated_index(self):
        # type: () -> dict
        """Successful migrations keyed by normalised source path.

        A path counts as migrated only when history says the migration
        succeeded, no rollback record exists for the job, and the journal (if
        still present) ended in a success state.  Older installs may have no
        journal left, so history alone is accepted in that case.
        """
        try:
            records = self.history.load()
        except Exception as exc:
            LOGGER.warning("migration history unavailable: %s", exc)
            return {}
        rolled_back = set(
            record.job_id
            for record in records
            if record.type == TYPE_ROLLBACK
            and record.result in (RESULT_ROLLED_BACK, RESULT_SUCCESS)
            and record.job_id
        )
        index = {}  # type: dict
        for record in records:
            if record.type != TYPE_MIGRATION or record.result != RESULT_SUCCESS:
                continue
            if not record.job_id or not record.source:
                continue
            if record.job_id in rolled_back:
                continue
            try:
                entry = self.journals.load(record.job_id, TYPE_MIGRATION)
            except Exception as exc:
                # History does not validate job-id format; a hand-edited or
                # legacy record must not abort the whole scan.
                LOGGER.warning("journal for %s is unusable: %s", record.job_id, exc)
                entry = None
            if entry is not None and entry.state not in (
                STATE_DONE,
                STATE_LINKED_CLEANUP_PENDING,
            ):
                continue
            key = _normalize_source_path(record.source)
            if not key:
                continue
            index[key] = {
                "job_id": record.job_id,
                "target": record.target
                or (entry.final if entry is not None else ""),
            }
        return index

    def _build_item(self, measured, engine, processes, migrated=None):
        # type: (object, object, object, object) -> dict
        classification = engine.classify(measured)
        process = processes.get(measured.tool_id)
        target_path = ""
        target_exists = False
        try:
            target_path = migrate_paths.final_path(
                self.layout.data_dir, measured.tool_id, measured.source_path
            )
            target_exists = os.path.isdir(target_path)
        except Exception:
            target_path = ""
        blocked_reason = ""
        blocked_code = ""
        migrated_job_id = ""
        if migrated:
            migrated_job_id = _text(migrated.get("job_id"))
            recorded_target = _text(migrated.get("target"))
            if recorded_target:
                target_path = recorded_target
                target_exists = os.path.isdir(recorded_target)
            blocked_code = "ALREADY_MIGRATED"
            blocked_reason = "已迁移到目标盘，不能重复迁移"
        elif measured.access_errors:
            blocked_code = "PERMISSION_DENIED"
            blocked_reason = "部分目录无法读取，无法安全迁移"
        elif measured.is_reparse:
            blocked_code = "BLOCKED_RED_PATH"
            blocked_reason = "源目录是链接/重解析点，不能迁移"
        elif process is not None and process.blocked_by_process:
            blocked_code = "BLOCKED_PROCESS"
            blocked_reason = "应用正在运行或进程状态未知"
        elif target_exists:
            blocked_code = "DUPLICATE_TARGET"
            blocked_reason = "目标位置已存在同名目录"
        elif classification.safety == SAFETY_RED:
            blocked_code = "BLOCKED_RED_PATH"
            blocked_reason = classification.reason or "禁止迁移路径"

        payload = {
            "item_id": measured.item_id,
            "tool_id": measured.tool_id,
            "tool_name": self._tool_names.get(measured.tool_id, measured.tool_id),
            "source_path": measured.source_path,
            "rel_path": measured.rel_path,
            "kind": measured.kind,
            "bytes": int(measured.bytes),
            "file_count": int(measured.file_count),
            "is_reparse": bool(measured.is_reparse),
            "access_errors": int(measured.access_errors),
            "mtime_max": measured.mtime_max,
            "target_path": target_path,
            "target_exists": target_exists,
            "safety": classification.safety,
            "source": classification.source,
            "rule_id": classification.rule_id,
            "reason": classification.reason,
            "reason_key": classification.reason_key,
            "requires_confirm": bool(classification.requires_confirm),
            "can_migrate": bool(classification.safety != SAFETY_RED and not blocked_reason),
            "blocked_code": blocked_code,
            "blocked_reason": blocked_reason,
            "process": process.to_dict() if process is not None else None,
            "migrated_job_id": migrated_job_id,
        }
        return payload

    def _build_tool_rows(self, discovery, items, processes):
        # type: (object, object, object) -> list
        rows = []
        for tool in discovery.tools:
            tool_items = [item for item in items if item["tool_id"] == tool.id]
            process = processes.get(tool.id)
            payload = tool.to_dict()
            payload.update(
                {
                    "size_bytes": sum(item["bytes"] for item in tool_items),
                    "file_count": sum(item["file_count"] for item in tool_items),
                    "item_count": len(tool_items),
                    "green_bytes": sum(
                        item["bytes"] for item in tool_items if item["safety"] == SAFETY_GREEN
                    ),
                    "yellow_bytes": sum(
                        item["bytes"] for item in tool_items if item["safety"] == SAFETY_YELLOW
                    ),
                    "red_bytes": sum(
                        item["bytes"] for item in tool_items if item["safety"] == SAFETY_RED
                    ),
                    "runtime": process.to_dict() if process is not None else None,
                    "blocked_by_process": bool(
                        process is not None and process.blocked_by_process
                    ),
                }
            )
            rows.append(payload)
        return rows

    def _stats(self, tools, items):
        # type: (list, list) -> dict
        present = [tool for tool in tools if tool.get("status") == STATUS_PRESENT]
        return {
            "tools": len(tools),
            "tools_present": len(present),
            "items": len(items),
            "bytes": sum(item["bytes"] for item in items),
            "files": sum(item["file_count"] for item in items),
            "green_bytes": sum(
                item["bytes"] for item in items if item["safety"] == SAFETY_GREEN
            ),
            "yellow_bytes": sum(
                item["bytes"] for item in items if item["safety"] == SAFETY_YELLOW
            ),
            "red_bytes": sum(
                item["bytes"] for item in items if item["safety"] == SAFETY_RED
            ),
            "migratable_bytes": sum(
                item["bytes"] for item in items if item.get("can_migrate")
            ),
            "blocked_items": sum(1 for item in items if item.get("blocked_code")),
            "access_errors": sum(item["access_errors"] for item in items),
        }

    def _build_engine(self):
        # type: () -> object
        learned = self.learned.load()
        return load_engine(
            self.rules_path,
            catalog_path=self.catalog_path,
            environ=self.environ,
            learned=learned,
            ai_resolver=self._build_ai_resolver(),
        )

    # -- planning / migration -------------------------------------------

    def _snapshot_items(self):
        # type: () -> OrderedDict
        with self._lock:
            snapshot = self._snapshot
        if snapshot is None:
            raise not_found("no scan result yet; run scan.start first")
        return OrderedDict((item["item_id"], item) for item in snapshot["items"])

    def _selected_items(self, item_ids):
        # type: (object) -> list
        if not isinstance(item_ids, list) or not item_ids:
            raise invalid("item_ids must be a non-empty array")
        known = self._snapshot_items()
        selected = []
        seen = set()
        for raw in item_ids:
            if not isinstance(raw, str) or not raw:
                raise invalid("item_ids must contain non-empty strings")
            if raw in seen:
                continue
            seen.add(raw)
            item = known.get(raw)
            if item is None:
                raise not_found("item not found in the current scan", item_id=raw)
            selected.append(item)
        if not selected:
            raise invalid("no items selected")
        return selected

    def plan_preview(self, item_ids, confirmed_item_ids=None):
        # type: (object, object) -> dict
        selected = self._selected_items(item_ids)
        confirmed = set(
            value for value in (confirmed_item_ids or []) if isinstance(value, str)
        )

        catalog = load_catalog(self.catalog_path)
        processes = detect_processes(catalog, process_provider=self._process_provider)
        names = {tool.id: tool.name for tool in catalog.tools}
        self._tool_names = names

        red = [item for item in selected if item["safety"] == SAFETY_RED]
        if red:
            raise BridgeError(
                "BLOCKED_RED_PATH",
                "red paths can never be migrated",
                {
                    "items": [
                        {
                            "item_id": item["item_id"],
                            "tool_id": item["tool_id"],
                            "source_path": item["source_path"],
                            "reason": item["reason"],
                        }
                        for item in red
                    ]
                },
            )

        already_migrated = [
            item for item in selected if item.get("migrated_job_id")
        ]
        if already_migrated:
            raise BridgeError(
                "ALREADY_MIGRATED",
                "some selected directories are already migrated",
                {
                    "items": [
                        {
                            "item_id": item["item_id"],
                            "tool_id": item["tool_id"],
                            "source_path": item["source_path"],
                            "job_id": item["migrated_job_id"],
                        }
                        for item in already_migrated
                    ]
                },
            )

        blocked_process = []
        for item in selected:
            process = processes.get(item["tool_id"])
            if process is not None and process.blocked_by_process:
                blocked_process.append(
                    {
                        "item_id": item["item_id"],
                        "tool_id": item["tool_id"],
                        "tool_name": names.get(item["tool_id"], item["tool_id"]),
                        "process": process.to_dict(),
                    }
                )
        if blocked_process:
            raise BridgeError(
                "BLOCKED_PROCESS",
                "close the application before migrating its data",
                {"items": blocked_process},
            )

        broken = [
            item
            for item in selected
            if item["access_errors"] or item["is_reparse"]
        ]
        if broken:
            raise BridgeError(
                "PERMISSION_DENIED",
                "some selected directories cannot be read safely",
                {
                    "items": [
                        {
                            "item_id": item["item_id"],
                            "source_path": item["source_path"],
                            "access_errors": item["access_errors"],
                            "is_reparse": item["is_reparse"],
                        }
                        for item in broken
                    ]
                },
            )

        duplicates = []
        groups = {}
        for item in selected:
            try:
                target = migrate_paths.final_path(
                    self.layout.data_dir, item["tool_id"], item["source_path"]
                )
            except Exception as exc:
                raise invalid("source path cannot be mapped: %s" % exc)
            key = os.path.normcase(target)
            groups.setdefault(key, []).append(item)
        for target, group in groups.items():
            if len(group) > 1:
                duplicates.append(
                    {
                        "target_path": target,
                        "items": [
                            {
                                "item_id": item["item_id"],
                                "source_path": item["source_path"],
                            }
                            for item in group
                        ],
                    }
                )
        existing = [
            {
                "item_id": item["item_id"],
                "source_path": item["source_path"],
                "target_path": group[0].get("target_path", ""),
            }
            for group in groups.values()
            for item in group[:1]
            if item.get("target_exists") or os.path.exists(group[0].get("target_path", ""))
        ]
        if duplicates or existing:
            raise BridgeError(
                "DUPLICATE_TARGET",
                "target already contains an entry with the same name",
                {"duplicates": duplicates, "existing": existing},
            )

        total_bytes = sum(item["bytes"] for item in selected)
        need = _need_bytes(total_bytes)
        free = (
            int(self._free_space_provider(self.layout.data_dir))
            if self._free_space_provider is not None
            else _free_bytes(self.layout.data_dir)
        )
        if os.name == "nt":
            info = None
            try:
                info = volume_info(self.layout.data_dir)
            except Exception:
                info = None
            if info is not None and not getattr(info, "is_ntfs", False):
                raise BridgeError(
                    "TARGET_NOT_NTFS",
                    "the target disk must be NTFS to create links",
                    {"target_root": self.layout.root, "fs": getattr(info, "fs_name", "")},
                )
        if free < need:
            raise BridgeError(
                "INSUFFICIENT_SPACE",
                "not enough free space on the target disk",
                {
                    "free_bytes": free,
                    "required_bytes": need,
                    "target_root": self.layout.root,
                },
            )

        yellow = [
            item for item in selected if item["safety"] == SAFETY_YELLOW
        ]
        unconfirmed = [item["item_id"] for item in yellow if item["item_id"] not in confirmed]
        plan_items = []
        for item in selected:
            plan_items.append(
                {
                    "item_id": item["item_id"],
                    "tool_id": item["tool_id"],
                    "tool_name": names.get(item["tool_id"], item["tool_id"]),
                    "source_path": item["source_path"],
                    "final_path": migrate_paths.final_path(
                        self.layout.data_dir, item["tool_id"], item["source_path"]
                    ),
                    "bytes": int(item["bytes"]),
                    "file_count": int(item["file_count"]),
                    "safety": item["safety"],
                    "requires_confirm": bool(
                        item["safety"] == SAFETY_YELLOW or item["requires_confirm"]
                    ),
                }
            )
        plan = self.plans.create(
            "migrate",
            {
                "kind": "migrate",
                "items": plan_items,
                "total_bytes": total_bytes,
                "need_bytes": need,
                "yellow_item_ids": [item["item_id"] for item in yellow],
            },
        )
        plan.update(
            {
                "items": plan_items,
                "total_bytes": total_bytes,
                "need_bytes": need,
                "free_bytes": free,
                "yellow_item_ids": [item["item_id"] for item in yellow],
                "unconfirmed_yellow": unconfirmed,
                "requires_confirm": bool(unconfirmed),
            }
        )
        return plan

    def migrate_start(self, plan_id, confirm_token, confirmed_item_ids=None):
        # type: (object, object, object) -> dict
        payload = self.plans.peek(plan_id) if isinstance(plan_id, str) else None
        if payload is None:
            raise invalid("plan_id is required")
        if payload.get("kind") != "migrate":
            raise invalid("plan is not a migration plan", plan_id=plan_id)
        confirmed = set(
            value for value in (confirmed_item_ids or []) if isinstance(value, str)
        )
        missing = [
            item_id
            for item_id in payload.get("yellow_item_ids", [])
            if item_id not in confirmed
        ]
        if missing:
            raise BridgeError(
                "CONFIRM_REQUIRED",
                "yellow items require explicit confirmation",
                {"item_ids": missing, "warning_key": "migrate.yellow_warning"},
            )
        job_id = self.runner.start(
            MIGRATE,
            self._make_migrate_worker(plan_id, confirm_token),
            title="migrate",
            check_busy=True,
        )
        return {"job_id": job_id, "state": "queued", "items": len(payload.get("items", []))}

    def _make_migrate_worker(self, plan_id, confirm_token):
        # type: (object, object) -> object
        def worker(job, token, emit):
            return self._migrate_worker(job, token, emit, plan_id, confirm_token)

        return worker

    def _migrate_worker(self, job, token, emit, plan_id, confirm_token):
        # type: (object, object, object, str, str) -> dict
        plan = self.plans.consume(plan_id, confirm_token)
        items = plan.get("items", [])
        results = []
        cancelled = False

        for index, item in enumerate(items):
            if token.is_set():
                cancelled = True
                break
            result = self._migrate_one(job, token, emit, index, len(items), item)
            results.append(result)
            if result.get("cancelled"):
                cancelled = True
                break

        summary = {
            "items": results,
            "succeeded": sum(1 for result in results if result.get("ok")),
            "failed": sum(1 for result in results if not result.get("ok")),
            "bytes": sum(int(result.get("bytes") or 0) for result in results),
            "cancelled": bool(cancelled or token.is_set()),
        }
        emit(
            "job.progress",
            {
                "stage": "done",
                "step": "done",
                "done": True,
                "items_total": len(items),
                "items_done": len(results),
                "succeeded": summary["succeeded"],
                "failed": summary["failed"],
            },
        )
        return summary

    def _migrate_one(self, job, token, emit, index, total, item):
        # type: (object, object, object, int, int, dict) -> dict
        tool_id = item["tool_id"]
        source_path = item["source_path"]
        item_id = item["item_id"]
        expected_bytes = int(item.get("bytes") or 0)
        expected_files = int(item.get("file_count") or 0)
        job_item_id = migrate_paths.new_job_id()

        target_path = migrate_paths.final_path(self.layout.data_dir, tool_id, source_path)
        staging = migrate_paths.staging_path(
            self.layout.data_dir, tool_id, job_item_id, source_path
        )
        tombstone = migrate_paths.tombstone_path(source_path, job_item_id)
        entry = self.journals.begin(
            job_item_id,
            TYPE_MIGRATION,
            tool_id=tool_id,
            source=source_path,
            final=target_path,
            staging=staging,
            tombstone=tombstone,
            backup_dir=self.layout.backup_dir,
            state=STATE_PLANNED,
            step="prepare",
            intent={
                "item_id": item_id,
                "bytes": expected_bytes,
                "files": expected_files,
                "safety": item.get("safety"),
                "runner_job_id": job.job_id,
                "batch_index": index,
                "batch_total": total,
            },
        )
        del entry

        emit(
            "job.progress",
            {
                "stage": "prepare",
                "step": "prepare",
                "item_id": item_id,
                "job_item_id": job_item_id,
                "tool_id": tool_id,
                "index": index,
                "items_total": total,
                "items_done": index,
            },
        )

        def progress(payload):
            data = dict(payload or {})
            data["step"] = data.get("stage") or "migrate"
            data.setdefault("item_id", item_id)
            data.setdefault("job_item_id", job_item_id)
            data.setdefault("index", index)
            data.setdefault("items_total", total)
            emit("job.progress", data, throttle=True)

        request = MigrationRequest(
            tool_id=tool_id,
            source_path=source_path,
            target_root=self.layout.data_dir,
            backup_root=self.layout.backup_dir,
            job_id=job_item_id,
            expected_bytes=expected_bytes,
            expected_files=expected_files,
        )

        try:
            prepared = prepare_copy(
                request,
                transfer=self._transfer,
                cancel_token=token,
                progress_cb=progress,
            )
        except MigrationCancelled:
            self.journals.set_result(
                job_item_id,
                STATE_FAILED,
                {"cancelled": True, "reason": "cancelled"},
                step="cancelled",
            )
            self._record_migration(
                item,
                job_item_id,
                target_path,
                RESULT_CANCELLED,
                STATE_FAILED,
                "cancelled",
                expected_bytes,
            )
            return self._item_result(item, job_item_id, False, STATE_FAILED, "cancelled", True)
        except Exception as exc:
            LOGGER.error("prepare failed for %s: %s\n%s", item_id, exc, traceback.format_exc())
            self.journals.set_result(
                job_item_id,
                STATE_FAILED,
                {"reason": str(exc)},
                step="prepare",
            )
            self._record_migration(
                item,
                job_item_id,
                target_path,
                RESULT_FAILURE,
                "prepare",
                str(exc),
                expected_bytes,
            )
            return self._item_result(item, job_item_id, False, STATE_FAILED, str(exc), False)

        # Record the per-job backup directory, not the configurable backup
        # root: rollback and recovery locate the manifest through this path.
        if prepared.backup_dir:
            self.journals.update(job_item_id, backup_dir=prepared.backup_dir)

        if not prepared.ok:
            self.journals.set_result(
                job_item_id,
                STATE_FAILED,
                prepared.to_dict(),
                step=prepared.error_code or "prepare",
            )
            self._record_migration(
                item,
                job_item_id,
                target_path,
                RESULT_FAILURE,
                prepared.error_code or "prepare",
                prepared.error_message or "",
                expected_bytes,
            )
            return self._item_result(
                item,
                job_item_id,
                False,
                STATE_FAILED,
                prepared.error_message or prepared.error_code or "",
                False,
            )

        self.journals.set_result(
            job_item_id,
            "COPY_VERIFIED",
            {
                "backup_dir": prepared.backup_dir or "",
                "staging_path": prepared.staging_path,
                "verify": prepared.verify.to_dict() if prepared.verify else None,
            },
            step="verify",
        )

        try:
            completion = complete_migration(
                prepared,
                process_names=self._process_names(tool_id),
                progress_cb=progress,
                cancel_token=token,
            )
        except MigrationCancelled:
            self.journals.set_result(
                job_item_id,
                STATE_FAILED,
                {"cancelled": True, "reason": "cancelled"},
                step="cancelled",
            )
            self._record_migration(
                item,
                job_item_id,
                target_path,
                RESULT_CANCELLED,
                STATE_FAILED,
                "cancelled",
                expected_bytes,
            )
            return self._item_result(item, job_item_id, False, STATE_FAILED, "cancelled", True)
        except Exception as exc:
            LOGGER.error(
                "complete failed for %s: %s\n%s", item_id, exc, traceback.format_exc()
            )
            self.journals.set_result(
                job_item_id, STATE_FAILED, {"reason": str(exc)}, step="complete"
            )
            self._record_migration(
                item,
                job_item_id,
                target_path,
                RESULT_FAILURE,
                "complete",
                str(exc),
                expected_bytes,
            )
            return self._item_result(item, job_item_id, False, STATE_FAILED, str(exc), False)

        self.journals.set_result(
            job_item_id,
            completion.state,
            completion.to_dict(),
            step=completion.error_code or completion.state,
        )

        if completion.ok:
            self._record_migration(
                item,
                job_item_id,
                target_path,
                RESULT_SUCCESS,
                completion.state,
                "",
                expected_bytes,
            )
            self._mark_migrated(item_id, job_item_id, target_path)
        elif completion.error_code == ERROR_CANCELLED:
            self._record_migration(
                item,
                job_item_id,
                target_path,
                RESULT_CANCELLED,
                completion.state,
                "cancelled",
                expected_bytes,
            )
        else:
            self._record_migration(
                item,
                job_item_id,
                target_path,
                RESULT_FAILURE,
                completion.error_code or completion.state,
                completion.error_message or "",
                expected_bytes,
            )

        return self._item_result(
            item,
            job_item_id,
            completion.ok,
            completion.state,
            completion.error_message or completion.error_code or "",
            completion.error_code == ERROR_CANCELLED,
            cleanup_pending=completion.cleanup_pending,
            target_path=target_path,
        )

    def _item_result(
        self,
        item,
        job_item_id,
        ok,
        state,
        message="",
        cancelled=False,
        cleanup_pending=False,
        target_path="",
    ):
        # type: (dict, str, bool, str, str, bool, bool, str) -> dict
        return {
            "item_id": item["item_id"],
            "tool_id": item["tool_id"],
            "tool_name": item.get("tool_name", item["tool_id"]),
            "source_path": item["source_path"],
            "target_path": target_path
            or migrate_paths.final_path(
                self.layout.data_dir, item["tool_id"], item["source_path"]
            ),
            "bytes": int(item.get("bytes") or 0),
            "job_id": job_item_id,
            "state": state,
            "state_label": STATE_LABELS.get(state, state),
            "ok": bool(ok),
            "cancelled": bool(cancelled),
            "cleanup_pending": bool(cleanup_pending),
            "error_message": message,
        }

    def _process_names(self, tool_id):
        # type: (str) -> tuple
        try:
            catalog = load_catalog(self.catalog_path)
        except Exception:
            return ()
        for tool in catalog.tools:
            if tool.id == tool_id:
                return tuple(tool.processes)
        return ()

    def _record_migration(self, item, job_id, target_path, result, stage, reason, size):
        # type: (dict, str, str, str, str, str, int) -> None
        try:
            self.history.record_migration(
                tool=item["tool_id"],
                item=os.path.basename(item["source_path"].rstrip("\\/")) or item["source_path"],
                source=item["source_path"],
                target=target_path,
                size=int(size),
                result=result,
                job_id=job_id,
                stage=stage,
                reason=reason or "",
            )
        except Exception as exc:
            LOGGER.error("history record failed for %s: %s", job_id, exc)

    def _mark_migrated(self, item_id, job_id, target_path):
        # type: (str, str, str) -> None
        with self._lock:
            if self._snapshot is None:
                return
            for item in self._snapshot["items"]:
                if item["item_id"] == item_id:
                    item["target_exists"] = True
                    item["migrated_job_id"] = job_id
                    item["target_path"] = target_path
                    item["blocked_code"] = "ALREADY_MIGRATED"
                    item["blocked_reason"] = "已迁移到目标盘，不能重复迁移"
                    item["can_migrate"] = False
                    break

    # -- rollback --------------------------------------------------------

    def _rollback_request_for(self, job_id):
        # type: (str) -> tuple
        entry = self.journals.load(job_id, TYPE_MIGRATION)
        if entry is None:
            raise not_found(
                "migration record not found for this job",
                job_id=job_id,
            )
        final_path = entry.final
        if not final_path:
            final_path = migrate_paths.final_path(
                self.layout.data_dir, entry.tool_id, entry.source
            )
        request = RollbackRequest(
            job_id=job_id,
            tool_id=entry.tool_id,
            source_path=entry.source,
            final_path=final_path,
            backup_dir=entry.backup_dir or None,
            process_names=self._process_names(entry.tool_id),
            expected_bytes=_as_int(entry.intent.get("bytes"), 0) or None,
            expected_files=_as_int(entry.intent.get("files"), 0) or None,
            config_choice="keep",
        )
        return entry, request

    def rollback_preview(self, job_id, confirm_residual_delete=False):
        # type: (object, object) -> dict
        if not isinstance(job_id, str) or not job_id:
            raise invalid("job_id is required")
        entry, request = self._rollback_request_for(job_id)
        if confirm_residual_delete:
            request.confirm_residual_delete = True
        precheck = precheck_rollback(
            request,
            process_provider=self._process_provider,
            free_space_provider=self._free_space_provider,
            jobs_dir=self.layout.jobs_dir,
        )
        residual_confirmed = (
            precheck.mode == STATE_RESIDUAL_COPY
            and bool(request.confirm_residual_delete)
        )
        can_proceed = bool(precheck.can_proceed) or residual_confirmed
        payload = {
            "job_id": job_id,
            "tool_id": entry.tool_id,
            "tool_name": self._tool_names.get(entry.tool_id, entry.tool_id),
            "source_path": entry.source,
            "final_path": request.final_path,
            "precheck": precheck.to_dict(),
            "can_proceed": can_proceed,
            "mode": precheck.mode,
            "error_code": precheck.error_code or "",
            "error_message": precheck.error_message or "",
            "requires_residual_confirm": (
                precheck.mode == STATE_RESIDUAL_COPY and not residual_confirmed
            ),
            "restore_bytes": int(precheck.target_bytes or 0),
            "free_bytes": precheck.free_bytes,
            "required_bytes": int(precheck.required_bytes or 0),
        }
        if can_proceed:
            plan = self.plans.create(
                "rollback",
                {
                    "kind": "rollback",
                    "job_id": job_id,
                    "tool_id": entry.tool_id,
                    "source_path": entry.source,
                    "final_path": request.final_path,
                    "backup_dir": request.backup_dir or "",
                    "process_names": list(request.process_names),
                    "expected_bytes": request.expected_bytes,
                    "expected_files": request.expected_files,
                    "confirm_residual_delete": bool(request.confirm_residual_delete),
                    "config_choice": request.config_choice,
                },
            )
            payload.update(
                {
                    "plan_id": plan["plan_id"],
                    "confirm_token": plan["confirm_token"],
                    "expires_at": plan["expires_at"],
                }
            )
        return payload

    def rollback_start(self, plan_id, confirm_token):
        # type: (object, object) -> dict
        if not isinstance(plan_id, str) or not plan_id:
            raise invalid("plan_id is required")
        payload = self.plans.peek(plan_id)
        if payload.get("kind") != "rollback":
            raise invalid("plan is not a rollback plan", plan_id=plan_id)
        job_id = self.runner.start(
            ROLLBACK,
            self._make_rollback_worker(plan_id, confirm_token),
            title="rollback",
            check_busy=True,
        )
        return {"job_id": job_id, "state": "queued", "target_job_id": payload.get("job_id")}

    def _make_rollback_worker(self, plan_id, confirm_token):
        # type: (object, object) -> object
        def worker(job, token, emit):
            return self._rollback_worker(job, token, emit, plan_id, confirm_token)

        return worker

    def _rollback_worker(self, job, token, emit, plan_id, confirm_token, plan=None):
        # type: (object, object, object, str, str, object) -> dict
        if plan is None:
            plan = self.plans.consume(plan_id, confirm_token)
        request = RollbackRequest(
            job_id=plan["job_id"],
            tool_id=plan["tool_id"],
            source_path=plan["source_path"],
            final_path=plan["final_path"],
            backup_dir=plan.get("backup_dir") or None,
            process_names=tuple(plan.get("process_names") or ()),
            expected_bytes=plan.get("expected_bytes"),
            expected_files=plan.get("expected_files"),
            confirm_residual_delete=bool(plan.get("confirm_residual_delete")),
            config_choice=plan.get("config_choice") or "keep",
        )

        def progress(payload):
            data = dict(payload or {})
            data["step"] = data.get("stage") or "rollback"
            data.setdefault("job_id", job.job_id)
            emit("job.progress", data, throttle=True)

        emit(
            "job.progress",
            {"stage": "precheck", "step": "precheck", "tool_id": request.tool_id},
        )
        result = rollback(
            request,
            transfer=self._transfer,
            cancel_token=token,
            progress_cb=progress,
            process_provider=self._process_provider,
            free_space_provider=self._free_space_provider,
            jobs_dir=self.layout.jobs_dir,
            history_path=self.layout.history_path,
            learned_path=self.layout.learned_path,
        )
        if result.ok:
            self._apply_rollback_to_snapshot(request.job_id, request.tool_id, request.source_path)
        summary = {
            "job_id": request.job_id,
            "runner_job_id": job.job_id,
            "tool_id": request.tool_id,
            "source_path": request.source_path,
            "final_path": request.final_path,
            "state": result.state,
            "state_label": STATE_LABELS.get(result.state, result.state),
            "ok": bool(result.ok),
            "error_code": result.error_code or "",
            "error_message": result.error_message or "",
            "data_kept": bool(result.data_kept),
            "target_removed": bool(result.target_removed),
            "source_restored": bool(result.source_restored),
        }
        emit(
            "job.progress",
            {
                "stage": "done",
                "step": "done",
                "done": True,
                "ok": bool(result.ok),
                "state": result.state,
            },
        )
        return summary

    def _apply_rollback_to_snapshot(self, job_id, tool_id, source_path):
        # type: (str, str, str) -> None
        # Rollback restores the directory to its pre-migration shape, so the
        # row must look exactly like a fresh scan: keep the classified safety
        # (migration never changes it) and only clear the migrated/blocked
        # flags.  Forcing SAFETY_RED here made the restored row read as
        # protected, excluding it from selection until the next scan.
        with self._lock:
            if self._snapshot is None:
                return
            for item in self._snapshot["items"]:
                if item.get("migrated_job_id") == job_id or (
                    item["tool_id"] == tool_id
                    and _normalize_source_path(item["source_path"])
                    == _normalize_source_path(source_path)
                ):
                    item["target_exists"] = False
                    item["migrated_job_id"] = ""
                    item["blocked_code"] = ""
                    item["blocked_reason"] = ""
                    item["can_migrate"] = item.get("safety") != SAFETY_RED
                    break
            snapshot = self._snapshot
        try:
            snapshot["stats"] = self._stats(snapshot["tools"], snapshot["items"])
        except Exception:
            pass

    # -- recovery --------------------------------------------------------

    def recovery_list(self):
        # type: () -> dict
        items = []
        try:
            entries = self.journals.list_all()
        except Exception as exc:
            LOGGER.warning("journal listing failed: %s", exc)
            return {"items": [], "count": 0, "warnings": [str(exc)]}
        # Migration records win the dedupe below: they carry the paths and the
        # rollback action the user needs, while a rollback record only offers
        # "recover".  Stable sort keeps newest-first order inside each group.
        entries = sorted(entries, key=lambda entry: 0 if entry.type == TYPE_MIGRATION else 1)
        ignored = {}
        try:
            ignored = self.journals.ignored()
        except Exception as exc:
            LOGGER.warning("ignored journal listing failed: %s", exc)

        seen = set()
        for entry in entries:
            # One row per job: a rollback record mirrors its migration record
            # and would otherwise double the count for the same folder.
            if entry.job_id in seen:
                continue
            if ignored.get("%s:%s" % (entry.type, entry.job_id)) == entry.state:
                # The user hid this exact state.  Any later transition makes
                # the entry visible again, so nothing new is ever swallowed.
                continue
            if entry.type == TYPE_MIGRATION:
                pending = entry.state not in MIGRATION_TERMINAL
                attention = entry.state in (
                    STATE_RECOVERY_REQUIRED,
                    STATE_LINKED_CLEANUP_PENDING,
                )
                if not pending and not attention:
                    continue
                if entry.state == STATE_LINKED_CLEANUP_PENDING:
                    actions = ["recover"]
                    label = "迁移已完成，仍有待清理副本"
                elif entry.state == STATE_RECOVERY_REQUIRED:
                    actions = ["recover", "rollback"]
                    label = "迁移状态需要人工确认"
                else:
                    actions = ["rollback", "recover"]
                    label = "上次迁移未完成，可一键回滚或继续恢复"
            elif entry.type == TYPE_ROLLBACK:
                if entry.state not in ROLLBACK_UNFINISHED:
                    continue
                actions = ["recover"]
                label = "上次回滚未完成"
            else:
                continue

            final_path = entry.final
            if not final_path:
                try:
                    final_path = migrate_paths.final_path(
                        self.layout.data_dir, entry.tool_id, entry.source
                    )
                except Exception:
                    final_path = ""

            seen.add(entry.job_id)
            items.append(
                {
                    "job_id": entry.job_id,
                    "type": entry.type,
                    "tool_id": entry.tool_id,
                    "tool_name": self._tool_names.get(entry.tool_id, entry.tool_id),
                    "source_path": entry.source,
                    "final_path": final_path,
                    "state": entry.state,
                    "state_label": STATE_LABELS.get(entry.state, entry.state),
                    "label": label,
                    "bytes": _as_int(entry.intent.get("bytes"), 0),
                    "created_at": entry.created_at,
                    "updated_at": entry.updated_at,
                    "recovered_at": entry.recovered_at,
                    "actions": list(actions),
                    "needs_human": entry.state
                    in (STATE_RECOVERY_REQUIRED, STATE_ROLLBACK_RECOVERY_REQUIRED),
                    "ignorable": entry.state in IGNORABLE_STATES,
                    "detail": self._recovery_detail(entry),
                }
            )
        items.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
        return {"items": items, "count": len(items), "warnings": []}

    @staticmethod
    def _recovery_detail(entry):
        # type: (object) -> str
        """Why the entry needs attention, in words the user can act on."""
        result = entry.result if isinstance(entry.result, dict) else {}
        for key in ("error_message", "message", "reason"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def recovery_ignore(self, job_id):
        # type: (object) -> dict
        """Hide one attention item without touching its data.

        The journal stays on disk and stays loadable: ignoring is a UI
        decision, not a resolution.  If the entry later moves to another
        state it becomes visible again, so a real new problem is never
        swallowed by an old dismissal.
        """
        if not isinstance(job_id, str) or not job_id:
            raise invalid("job_id is required")
        entry = self.journals.load(job_id, TYPE_MIGRATION)
        if entry is None:
            entry = self.journals.load(job_id, TYPE_ROLLBACK)
        if entry is None:
            raise not_found("journal entry not found", job_id=job_id)
        if entry.state not in IGNORABLE_STATES:
            raise invalid(
                "this entry still has an automatic path; recover or roll it back",
                state=entry.state,
            )
        self.journals.ignore(job_id, entry.type, entry.state)
        LOGGER.info(
            "recovery item hidden by user: job=%s type=%s state=%s",
            job_id,
            entry.type,
            entry.state,
        )
        return {"job_id": job_id, "type": entry.type, "state": entry.state}

    def recovery_run(self, job_id, action="recover"):
        # type: (object, object) -> dict
        if not isinstance(job_id, str) or not job_id:
            raise invalid("job_id is required")
        if action not in ("recover", "rollback"):
            raise invalid("action must be recover or rollback")
        entry = self.journals.load(job_id, TYPE_MIGRATION)
        entry_type = TYPE_MIGRATION
        if entry is None:
            entry = self.journals.load(job_id, TYPE_ROLLBACK)
            entry_type = TYPE_ROLLBACK
        if entry is None:
            raise not_found("journal entry not found", job_id=job_id)
        if action == "rollback" and entry.type != TYPE_MIGRATION:
            raise invalid("only a migration can be rolled back")

        worker = self._make_recovery_worker(entry_type, action, job_id)
        runner_job = self.runner.start(
            RECOVER,
            worker,
            title="recover %s" % job_id,
            check_busy=True,
        )
        return {"job_id": runner_job, "state": "queued", "target_job_id": job_id, "action": action}

    @staticmethod
    def _source_is_junction(source_path):
        # type: (str) -> bool
        try:
            return bool(source_path) and bool(is_junction(source_path))
        except Exception:
            # An unreadable path is not evidence of a link; recovery is the
            # safe route because it never deletes unproven data.
            return False

    def _make_recovery_worker(self, entry_type, action, job_id):
        # type: (str, str, str) -> object
        def worker(job, token, emit):
            return self._recover_worker(job, token, emit, entry_type, action, job_id)

        return worker

    def _recover_worker(self, job, token, emit, entry_type, action, target_job_id):
        # type: (object, object, object, str, str, str) -> dict
        emit(
            "job.progress",
            {"stage": "recover", "step": "recover", "started": True},
        )

        if entry_type == TYPE_ROLLBACK:
            return self._recover_rollback_entry(target_job_id, token, emit)

        entry = self.journals.load(target_job_id, TYPE_MIGRATION)
        if entry is None:
            raise not_found("migration record disappeared", job_id=target_job_id)

        if action == "rollback" and not self._source_is_junction(entry.source):
            # Nothing is linked yet, so there is no junction for the rollback
            # engine to undo and it would stop at "source is already a real
            # directory".  Recovery is the rollback in this state: it restores
            # the source and removes only this job's own copy.
            return self._recover_worker(
                job, token, emit, entry_type, "recover", target_job_id
            )

        if action == "rollback":
            summary = self._rollback_worker(
                job,
                token,
                emit,
                "",
                "",
                plan={
                    "job_id": entry.job_id,
                    "tool_id": entry.tool_id,
                    "source_path": entry.source,
                    "final_path": entry.final
                    or migrate_paths.final_path(
                        self.layout.data_dir, entry.tool_id, entry.source
                    ),
                    "backup_dir": entry.backup_dir or "",
                    "process_names": list(self._process_names(entry.tool_id)),
                    "expected_bytes": _as_int(entry.intent.get("bytes"), 0) or None,
                    "expected_files": _as_int(entry.intent.get("files"), 0) or None,
                    "confirm_residual_delete": False,
                    "config_choice": "keep",
                },
            )
            if summary.get("ok"):
                self._close_migration_after_rollback(entry, summary)
            return summary

        def progress(payload):
            data = dict(payload or {})
            data["step"] = data.get("stage") or "recover"
            emit("job.progress", data, throttle=True)

        final_path = entry.final or migrate_paths.final_path(
            self.layout.data_dir, entry.tool_id, entry.source
        )
        completion = recover_job(
            entry.job_id,
            entry.tool_id,
            entry.source,
            final_path,
            staging_path=entry.staging or None,
            recorded_state=entry.state,
            progress_cb=progress,
        )
        try:
            self.journals.mark_recovered(
                entry.job_id, completion.state, completion.to_dict()
            )
        except Exception as exc:
            LOGGER.warning("could not mark migration recovered: %s", exc)

        if completion.state == STATE_ROLLED_BACK:
            self._reconcile_rollback_history(entry, completion)
            if not os.path.lexists(final_path):
                self._apply_rollback_to_snapshot(
                    entry.job_id, entry.tool_id, entry.source
                )
        elif completion.ok:
            self._reconcile_success_history(entry, completion)

        # A recovery that undoes the interrupted migration has resolved it:
        # the source is intact again and nothing is pending.  ``ok`` follows
        # the migration completion, so a rolled-back recovery used to report
        # failure and the UI banner said "恢复失败" after a successful undo.
        resolved = bool(completion.ok) or bool(completion.rolled_back) or (
            completion.state in (STATE_ROLLED_BACK, STATE_ALREADY_ROLLED_BACK)
        )
        summary = {
            "job_id": entry.job_id,
            "runner_job_id": job.job_id,
            "tool_id": entry.tool_id,
            "source_path": entry.source,
            "final_path": final_path,
            "state": completion.state,
            "state_label": STATE_LABELS.get(completion.state, completion.state),
            "ok": resolved,
            "rolled_back": bool(completion.rolled_back),
            "error_code": completion.error_code or "",
            "error_message": completion.error_message or "",
            "cleanup_pending": bool(completion.cleanup_pending),
        }
        emit(
            "job.progress",
            {
                "stage": "done",
                "step": "done",
                "done": True,
                "ok": resolved,
                "state": completion.state,
            },
        )
        return summary

    def _close_migration_after_rollback(self, entry, summary):
        # type: (object, dict) -> None
        # The rollback engine keeps its own journal and never writes the
        # migration record, so a successful rollback would otherwise leave the
        # interrupted migration pending: the banner reappears on every refresh
        # and the same entry can never be cleared.  Closing it here is what
        # makes "rollback" a real end state for the user.
        final_path = (
            entry.final
            or summary.get("final_path", "")
            or ""
        )
        if not self.history.was_rolled_back(entry.job_id):
            try:
                self.history.record_rollback(
                    tool=entry.tool_id,
                    item=os.path.basename(entry.source.rstrip("\\/")) or entry.source,
                    source=entry.source,
                    target=final_path,
                    size=_as_int(entry.intent.get("bytes"), 0),
                    job_id=entry.job_id,
                    stage=STATE_ROLLED_BACK,
                    reason="recovered",
                )
            except Exception as exc:
                LOGGER.warning("rollback history failed: %s", exc)
        try:
            self.journals.mark_recovered(
                entry.job_id,
                STATE_ROLLED_BACK,
                {
                    "state": STATE_ROLLED_BACK,
                    "error_code": "",
                    "error_message": "",
                    "message": "rolled back from the recovery list",
                    "rolled_back": True,
                    "rollback_state": summary.get("state", ""),
                },
            )
        except Exception as exc:
            LOGGER.warning("could not close migration journal: %s", exc)
        if final_path and not os.path.lexists(final_path):
            self._apply_rollback_to_snapshot(
                entry.job_id, entry.tool_id, entry.source
            )

    def _recover_rollback_entry(self, job_id, token, emit):
        # type: (str, object, object) -> dict
        entry = self.journals.load(job_id, TYPE_ROLLBACK)
        if entry is None:
            raise not_found("rollback record disappears", job_id=job_id)

        def progress(payload):
            data = dict(payload or {})
            data["step"] = data.get("stage") or "recover"
            emit("job.progress", data, throttle=True)

        result = recover_rollback(
            job_id,
            entry.tool_id,
            entry.source,
            entry.final
            or migrate_paths.final_path(
                self.layout.data_dir, entry.tool_id, entry.source
            ),
            backup_dir=entry.backup_dir or "",
            recorded_state=entry.state,
            jobs_dir=self.layout.jobs_dir,
            progress_cb=progress,
            history_path=self.layout.history_path,
            learned_path=self.layout.learned_path,
        )
        try:
            self.journals.mark_recovered(
                job_id, result.state, result.to_dict(), entry_type=TYPE_ROLLBACK
            )
        except Exception as exc:
            LOGGER.warning("could not mark rollback recovered: %s", exc)
        if result.ok:
            self._apply_rollback_to_snapshot(job_id, entry.tool_id, entry.source)
        summary = {
            "job_id": job_id,
            "runner_job_id": "",
            "tool_id": entry.tool_id,
            "source_path": entry.source,
            "final_path": entry.final,
            "state": result.state,
            "state_label": STATE_LABELS.get(result.state, result.state),
            "ok": bool(result.ok),
            "error_code": result.error_code or "",
            "error_message": result.error_message or "",
            "data_kept": bool(result.data_kept),
        }
        emit(
            "job.progress",
            {
                "stage": "done",
                "step": "done",
                "done": True,
                "ok": bool(result.ok),
                "state": result.state,
            },
        )
        return summary

    def _reconcile_success_history(self, entry, completion):
        # type: (object, object) -> None
        if self.history.find(entry.job_id, TYPE_MIGRATION):
            return
        try:
            self.history.record_migration(
                tool=entry.tool_id,
                item=os.path.basename(entry.source.rstrip("\\/")) or entry.source,
                source=entry.source,
                target=completion.final_path,
                size=_as_int(entry.intent.get("bytes"), 0),
                result=RESULT_SUCCESS,
                job_id=entry.job_id,
                stage=completion.state,
                reason="recovered",
            )
        except Exception as exc:
            LOGGER.warning("recovery history failed: %s", exc)

    def _reconcile_rollback_history(self, entry, completion):
        # type: (object, object) -> None
        if self.history.was_rolled_back(entry.job_id):
            return
        try:
            self.history.record_rollback(
                tool=entry.tool_id,
                item=os.path.basename(entry.source.rstrip("\\/")) or entry.source,
                source=entry.source,
                target=completion.final_path,
                size=_as_int(entry.intent.get("bytes"), 0),
                job_id=entry.job_id,
                stage=completion.state,
                reason="recovered",
            )
        except Exception as exc:
            LOGGER.warning("rollback recovery history failed: %s", exc)

    # -- history / rules -------------------------------------------------

    def history_list(self, filter="all", limit=500):
        # type: (object, object) -> dict
        wanted = filter if filter in ("all", "migrate", "rollback") else "all"
        try:
            records = self.history.load()
        except Exception as exc:
            LOGGER.warning("history load failed: %s", exc)
            records = []
        rolled_back = set(
            record.job_id
            for record in records
            if record.type == TYPE_ROLLBACK
            and record.result in (RESULT_ROLLED_BACK, RESULT_SUCCESS)
        )
        journal_cache = {}
        items = []
        for record in reversed(records):
            ui_type = "migrate" if record.type == TYPE_MIGRATION else "rollback"
            if wanted != "all" and ui_type != wanted:
                continue
            duration = None
            can_rollback = False
            if record.type == TYPE_MIGRATION:
                if record.job_id not in journal_cache:
                    journal_cache[record.job_id] = self.journals.load(
                        record.job_id, TYPE_MIGRATION
                    )
                journal = journal_cache[record.job_id]
                if journal is not None:
                    duration = _seconds_between(journal.created_at, journal.updated_at)
                    can_rollback = (
                        record.result == RESULT_SUCCESS
                        and journal.state in (STATE_DONE, STATE_LINKED_CLEANUP_PENDING)
                        and record.job_id not in rolled_back
                    )
            items.append(
                {
                    "id": record.job_id or ("%s-%d" % (record.type, len(items))),
                    "at": record.time,
                    "type": ui_type,
                    "tool_id": record.tool,
                    "app": self._tool_names.get(record.tool, record.tool),
                    "item": record.item,
                    "from": record.source,
                    "to": record.target,
                    "freed": int(record.bytes or 0),
                    "duration_seconds": duration,
                    "result": self._ui_result(record.result),
                    "raw_result": record.result,
                    "can_rollback": can_rollback,
                    "code": record.stage if record.result != RESULT_SUCCESS else "",
                    "note": record.reason,
                    "stage": record.stage,
                    "job_id": record.job_id,
                }
            )
            try:
                max_items = max(1, min(int(limit or 500), 5000))
            except (TypeError, ValueError):
                max_items = 500
            if len(items) >= max_items:
                break
        return {
            "items": items,
            "count": len(items),
            "stats": {
                "total": len(records),
                "rolled_back": len(rolled_back),
                "complete": bool(self.history.complete),
            },
        }

    @staticmethod
    def _ui_result(result):
        # type: (str) -> str
        if result == RESULT_SUCCESS:
            return "success"
        if result == RESULT_ROLLED_BACK:
            return "rolledback"
        return "failed"

    def rules_learned_list(self):
        # type: () -> dict
        rules = self.learned.load()
        return {
            "items": [
                {
                    "item_id": item_id,
                    "safety": rule.get("safety", ""),
                    "reason": rule.get("reason", ""),
                    "rule_id": rule.get("rule_id", ""),
                }
                for item_id, rule in sorted(rules.items())
            ],
            "warnings": list(self.learned.warnings),
        }

    def rules_override(self, item_id, safety, reason=""):
        # type: (object, object, object) -> dict
        if not isinstance(item_id, str) or not item_id:
            raise invalid("item_id is required")
        if safety not in (SAFETY_RED, SAFETY_GREEN):
            raise invalid("safety must be red or green")
        if not isinstance(reason, str):
            raise invalid("reason must be a string")
        changed = self.learned.set_rule(item_id, safety, reason, rule_id="manual")
        self._reclassify_snapshot()
        return {"item_id": item_id, "safety": safety, "changed": bool(changed)}

    def rules_override_clear(self, item_id):
        # type: (object) -> dict
        """Drop a manual override so the item returns to automatic rules."""
        if not isinstance(item_id, str) or not item_id:
            raise invalid("item_id is required")
        current = self.learned.load().get(item_id)
        if current is None or current.get("rule_id") != "manual":
            raise not_found("no manual override for this item", item_id=item_id)
        self.learned.remove(item_id)
        self._reclassify_snapshot()
        return {"item_id": item_id, "cleared": True}

    def _reclassify_snapshot(self):
        # type: () -> None
        with self._lock:
            snapshot = self._snapshot
        if snapshot is None:
            return
        try:
            engine = self._build_engine()
        except Exception as exc:
            LOGGER.warning("reclassification skipped: %s", exc)
            return

        class _Measured(object):
            def __init__(self, payload):
                self.item_id = payload["item_id"]
                self.tool_id = payload["tool_id"]
                self.source_path = payload["source_path"]
                self.rel_path = payload.get("rel_path") or ""
                self.kind = payload.get("kind") or "dir"

        with self._lock:
            for item in snapshot["items"]:
                try:
                    classification = engine.classify(_Measured(item))
                except Exception:
                    continue
                item["safety"] = classification.safety
                item["source"] = classification.source
                item["rule_id"] = classification.rule_id
                item["reason"] = classification.reason
                item["reason_key"] = classification.reason_key
                item["requires_confirm"] = bool(classification.requires_confirm)
                if not item.get("blocked_reason"):
                    item["can_migrate"] = classification.safety != SAFETY_RED
            if self._snapshot is not None:
                self._snapshot["stats"] = self._stats(
                    self._snapshot["tools"], self._snapshot["items"]
                )


def create_service(**kwargs):
    # type: (object) -> AppService
    """Convenience constructor used by the launcher and the tests."""
    return AppService(**kwargs)


__all__ = [
    "AppService",
    "DEFAULT_CATALOG_PATH",
    "DEFAULT_RULES_PATH",
    "STATE_LABELS",
    "create_service",
]
