# -*- coding: utf-8 -*-
"""JSON-RPC bridge exposed to the web UI through pywebview (architecture §5.1).

``window.pywebview.api.rpc(requestJson)`` takes one JSON string and returns
one JSON string:

    request  {"v": 1, "id": "42", "method": "scan.start", "params": {}}
    success  {"v": 1, "id": "42", "ok": true, "result": {...}}
    failure  {"v": 1, "id": "42", "ok": false, "error": {...}}

Only the methods in ``METHODS`` can be reached; the browser never gets a
filesystem path into a write, it gets ``item_id``/``plan_id``/``job_id`` back
from :class:`~src.app.service.AppService`.

Python 3.8 compatible (decision 0005).
"""

import json
import logging
import traceback

from . import PROTOCOL_VERSION
from .errors import BridgeError, invalid

LOGGER = logging.getLogger("disk_warden.app.bridge")

MAX_REQUEST_BYTES = 256 * 1024


def _params(request):
    # type: (dict) -> dict
    params = request.get("params")
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise invalid("params must be an object")
    return params


def _require_str(params, key):
    # type: (dict, str) -> str
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise invalid("%s is required" % key)
    return value


def _optional_str(params, key, default=""):
    # type: (dict, str, str) -> str
    value = params.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise invalid("%s must be a string" % key)
    return value


def _optional_bool(params, key, default=False):
    # type: (dict, str, bool) -> bool
    value = params.get(key, default)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise invalid("%s must be a boolean" % key)
    return value


def _string_list(params, key, required=False):
    # type: (dict, str, bool) -> list
    value = params.get(key)
    if value is None:
        if required:
            raise invalid("%s is required" % key)
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise invalid("%s must be an array of strings" % key)
    return list(value)


def _settings_patch(params):
    # type: (dict) -> dict
    patch = params.get("patch")
    if isinstance(patch, dict):
        return patch
    return {
        key: value
        for key, value in params.items()
        if key not in ("id", "method", "params")
    }


def build_methods(service):
    # type: (object) -> dict
    """Map bridge method names to thin service calls.

    Keeping the table explicit is what makes the whitelist auditable; adding
    a method requires touching this function.
    """

    return {
        "app.hello": lambda p: service.hello(),
        "app.ping": lambda p: {
            "ok": True,
            "protocol": PROTOCOL_VERSION,
        },
        "app.reveal": lambda p: service.reveal(_require_str(p, "path")),
        "app.open_external": lambda p: service.open_external(
            _require_str(p, "url")
        ),
        "settings.get": lambda p: service.settings_get(),
        "settings.set": lambda p: service.settings_set(_settings_patch(p)),
        "ai.status": lambda p: service.ai_status(),
        "ai.consent": lambda p: service.ai_consent(),
        "auth.status": lambda p: service.auth_status(),
        "auth.start": lambda p: service.auth_start(_require_str(p, "provider")),
        "auth.cancel": lambda p: service.auth_cancel(),
        "auth.sign_out": lambda p: service.auth_sign_out(),
        "welcome.seen": lambda p: service.welcome_seen(),
        "star.show": lambda p: service.star_show(),
        "star.dismiss": lambda p: service.star_dismiss(),
        "star.give": lambda p: service.star_give(),
        "star.check": lambda p: service.star_check(),
        "disk.info": lambda p: service.disk_info(),
        "scan.start": lambda p: service.scan_start(),
        "scan.cancel": lambda p: service.scan_cancel(p.get("job_id")),
        "scan.status": lambda p: service.scan_status(p.get("job_id")),
        "scan.result": lambda p: service.scan_result(),
        "items.list": lambda p: service.items_list(
            p.get("tool_id"), p.get("safety")
        ),
        "plan.preview": lambda p: service.plan_preview(
            _string_list(p, "item_ids", required=True),
            _string_list(p, "confirmed_item_ids"),
        ),
        "migrate.start": lambda p: service.migrate_start(
            _require_str(p, "plan_id"),
            _require_str(p, "confirm_token"),
            _string_list(p, "confirmed_item_ids"),
        ),
        "job.status": lambda p: service.job_status(_require_str(p, "job_id")),
        "job.cancel": lambda p: service.job_cancel(_require_str(p, "job_id")),
        "job.list": lambda p: service.job_list(_optional_bool(p, "active_only")),
        "rollback.preview": lambda p: service.rollback_preview(
            _require_str(p, "job_id"),
            _optional_bool(p, "confirm_residual_delete"),
        ),
        "rollback.start": lambda p: service.rollback_start(
            _require_str(p, "plan_id"),
            _require_str(p, "confirm_token"),
        ),
        "recovery.list": lambda p: service.recovery_list(),
        "recovery.run": lambda p: service.recovery_run(
            _require_str(p, "job_id"),
            _optional_str(p, "action", "recover") or "recover",
        ),
        "history.list": lambda p: service.history_list(
            _optional_str(p, "filter", "all") or "all",
            p.get("limit", 500),
        ),
        "rules.learned.list": lambda p: service.rules_learned_list(),
        "rules.override": lambda p: service.rules_override(
            _require_str(p, "item_id"),
            _require_str(p, "safety"),
            _optional_str(p, "reason", ""),
        ),
        "rules.override.clear": lambda p: service.rules_override_clear(
            _require_str(p, "item_id")
        ),
        "events.poll": lambda p: service.events_poll(p.get("since_seq", 0)),
    }


class Bridge(object):
    """The object handed to ``pywebview.create_window(js_api=...)``."""

    def __init__(self, service, methods=None):
        # type: (object, object) -> None
        self.service = service
        self.methods = build_methods(service) if methods is None else dict(methods)

    def rpc(self, request_json):
        # type: (object) -> str
        """Entry point called by the page; always returns a JSON string."""
        if isinstance(request_json, str) and len(request_json) > MAX_REQUEST_BYTES:
            return json.dumps(
                _error_response(None, invalid("request is too large")),
                ensure_ascii=False,
            )
        response = handle_request(self.service, request_json, self.methods)
        try:
            return json.dumps(response, ensure_ascii=False, default=_json_default)
        except (TypeError, ValueError) as exc:
            LOGGER.error("bridge response is not JSON serialisable: %s", exc)
            fallback = _error_response(
                response.get("id"),
                BridgeError("INTERNAL", "response could not be serialised"),
            )
            return json.dumps(fallback, ensure_ascii=False)


def _json_default(value):
    # type: (object) -> object
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    raise TypeError("not JSON serialisable: %r" % type(value))


def _error_response(request_id, error):
    # type: (object, BridgeError) -> dict
    return {
        "v": PROTOCOL_VERSION,
        "id": request_id,
        "ok": False,
        "error": error.to_error(),
    }


def handle_request(service, request, methods=None):
    # type: (object, object, object) -> dict
    """Validate and dispatch one decoded request, returning the envelope."""
    if methods is None:
        methods = build_methods(service)

    if isinstance(request, str):
        try:
            request = json.loads(request)
        except ValueError as exc:
            return _error_response(None, invalid("request is not valid JSON: %s" % exc))
    if not isinstance(request, dict):
        return _error_response(None, invalid("request must be an object"))

    request_id = request.get("id")
    version = request.get("v", PROTOCOL_VERSION)
    if version != PROTOCOL_VERSION:
        return _error_response(
            request_id,
            invalid("unsupported protocol version", expected=PROTOCOL_VERSION),
        )

    method = request.get("method")
    if not isinstance(method, str) or not method:
        return _error_response(request_id, invalid("method is required"))

    handler = methods.get(method)
    if handler is None:
        return _error_response(
            request_id,
            invalid("unknown method", method=method),
        )

    try:
        params = _params(request)
        result = handler(params)
    except BridgeError as exc:
        return _error_response(request_id, exc)
    except Exception as exc:  # never leak a traceback to the page
        LOGGER.error(
            "bridge method %s failed: %s\n%s",
            method,
            exc,
            traceback.format_exc(),
        )
        return _error_response(
            request_id,
            BridgeError("INTERNAL", "%s: %s" % (type(exc).__name__, exc)),
        )

    return {
        "v": PROTOCOL_VERSION,
        "id": request_id,
        "ok": True,
        "result": result,
    }


__all__ = ["Bridge", "build_methods", "handle_request"]
