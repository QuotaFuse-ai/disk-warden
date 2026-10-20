# -*- coding: utf-8 -*-
"""Bridge error envelope (architecture §5.4)."""

# code -> retryable
ERROR_CODES = {
    "INVALID_ARGUMENT": False,
    "FORBIDDEN": False,
    "NOT_FOUND": False,
    "BUSY": True,
    "JOB_CONFLICT": False,
    "BLOCKED_PROCESS": True,
    "BLOCKED_RED_PATH": False,
    "INSUFFICIENT_SPACE": True,
    "TARGET_NOT_NTFS": False,
    "PERMISSION_DENIED": True,
    "IO_ERROR": True,
    "VERIFY_FAILED": True,
    "LINK_FAILED": True,
    "ROLLBACK_FAILED": False,
    "RECOVERY_REQUIRED": False,
    "CONFIRM_REQUIRED": False,
    "DUPLICATE_TARGET": False,
    "AI_CONSENT_REQUIRED": False,
    "INTERNAL": False,
}

MESSAGE_KEYS = {
    code: "error." + code for code in ERROR_CODES
}


class BridgeError(Exception):
    """A failure that is safe to return to the UI.

    ``detail`` is always structured (a mapping); localised text lives in the
    UI behind ``message_key``.  ``reason`` is put in ``detail["reason"]`` so
    the UI can show a useful line even when it has no translation yet.
    """

    def __init__(self, code, reason="", detail=None, retryable=None):
        # type: (str, str, object, object) -> None
        if code not in ERROR_CODES:
            code = "INTERNAL"
        self.code = code
        self.reason = reason or ""
        self.detail = dict(detail) if isinstance(detail, dict) else {}
        if self.reason:
            self.detail.setdefault("reason", self.reason)
        self.retryable = (
            ERROR_CODES[code] if retryable is None else bool(retryable)
        )
        super(BridgeError, self).__init__(self.reason or code)

    @property
    def message_key(self):
        # type: () -> str
        return MESSAGE_KEYS[self.code]

    def to_error(self):
        # type: () -> dict
        return {
            "code": self.code,
            "message_key": self.message_key,
            "detail": self.detail,
            "retryable": self.retryable,
        }


def invalid(reason, **detail):
    # type: (str, object) -> BridgeError
    return BridgeError("INVALID_ARGUMENT", reason, detail)


def not_found(reason, **detail):
    # type: (str, object) -> BridgeError
    return BridgeError("NOT_FOUND", reason, detail)
