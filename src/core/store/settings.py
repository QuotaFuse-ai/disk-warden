# -*- coding: utf-8 -*-
"""User settings persistence (requirements 4.1 / AC-STORE-01/02).

The store validates known keys, keeps unknown keys for forward compatibility,
writes atomically with a ``.bak`` fallback, and never raises on a damaged file
unless the caller explicitly asks for strict validation.

Python 3.8 compatible (decision 0005).
"""

import copy
import os

from .atomic import (
    StoreCorruptError,
    StoreError,
    atomic_write_json,
    quarantine_corrupt,
    read_json_file,
)

SCHEMA_VERSION = 1
UNINSTALL_POLICIES = ("rollback", "keep")
DEFAULT_SETTINGS = {
    "schema_version": SCHEMA_VERSION,
    "launch_at_startup": False,
    "data_root": "",
    "uninstall_policy": "keep",
    "language": "en",
    "auto_maintenance": False,
}
_BOOL_KEYS = ("launch_at_startup", "auto_maintenance")
_STRING_KEYS = ("data_root", "language")
_BOOL = object()


def default_settings():
    # type: () -> dict
    return copy.deepcopy(DEFAULT_SETTINGS)


def _known_spec():
    # type: () -> dict
    spec = {key: _BOOL for key in _BOOL_KEYS}
    spec.update({key: str for key in _STRING_KEYS})
    spec["uninstall_policy"] = "policy"
    return spec


def validate_settings(payload, strict=False):
    # type: (object, bool) -> tuple
    """Return ``(settings, problems)``.

    Missing or invalid known keys fall back to their defaults.  With
    ``strict=True`` the first problem raises ``StoreError`` (used by write).
    Unknown keys are preserved.
    """
    problems = []
    result = default_settings()
    if payload is None:
        return result, problems
    if not isinstance(payload, dict):
        problems.append("settings must be a JSON object")
        if strict:
            raise StoreError(problems[0])
        return result, problems

    for key, value in payload.items():
        if key == "schema_version":
            continue
        if key not in _known_spec():
            result[key] = value
            continue
        if key == "uninstall_policy":
            if value in UNINSTALL_POLICIES:
                result[key] = value
            else:
                problems.append(
                    "uninstall_policy must be one of %s"
                    % (", ".join(UNINSTALL_POLICIES))
                )
        elif key in _BOOL_KEYS:
            if isinstance(value, bool):
                result[key] = value
            else:
                problems.append("%s must be a boolean" % key)
        else:
            if isinstance(value, str):
                result[key] = value
            else:
                problems.append("%s must be a string" % key)

    if strict and problems:
        raise StoreError("; ".join(problems))

    version = payload.get("schema_version")
    if isinstance(version, int) and not isinstance(version, bool) and version > 0:
        result["schema_version"] = max(SCHEMA_VERSION, version)
    return result, problems


class SettingsStore(object):
    """Settings file at ``<config>/settings.json``."""

    def __init__(self, path):
        # type: (str) -> None
        if not path:
            raise StoreError("settings path is required")
        self.path = os.path.abspath(os.path.normpath(path))
        self.backup_path = self.path + ".bak"
        self.warnings = []  # type: list
        self.needs_repair = False

    def load(self, repair=False):
        # type: (bool) -> dict
        """Load settings, falling back to ``.bak`` then defaults."""
        self.warnings = []
        self.needs_repair = False
        payload = None
        try:
            payload = read_json_file(self.path)
        except FileNotFoundError:
            # First run: defaults are returned and, when the caller asks for
            # repair/initialisation, written out (AC-STORE-01).
            self.needs_repair = True
            payload = None
            self.warnings.append(
                {
                    "code": "settings_defaults",
                    "message": "settings file missing; defaults applied",
                }
            )
        except StoreCorruptError as exc:
            self.needs_repair = True
            quarantined = quarantine_corrupt(self.path)
            self.warnings.append(
                {
                    "code": "settings_corrupt",
                    "message": str(exc),
                    "quarantined": quarantined,
                }
            )
            payload = self._load_backup()

        settings, problems = validate_settings(payload)
        for problem in problems:
            self.warnings.append({"code": "settings_invalid", "message": problem})
        if settings.get("schema_version") != SCHEMA_VERSION:
            # Keep a newer schema version, but mark an older file for repair.
            if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
                self.needs_repair = True
        elif isinstance(payload, dict):
            raw_version = payload.get("schema_version")
            if (
                isinstance(raw_version, int)
                and not isinstance(raw_version, bool)
                and raw_version < SCHEMA_VERSION
            ):
                self.needs_repair = True
        if repair and self.needs_repair:
            try:
                settings = self.save(settings)
            except StoreError as exc:
                self.warnings.append(
                    {"code": "settings_repair_failed", "message": str(exc)}
                )
        return settings

    def _load_backup(self):
        # type: () -> object
        try:
            payload = read_json_file(self.backup_path)
        except FileNotFoundError:
            self.warnings.append(
                {
                    "code": "settings_defaults",
                    "message": "settings file missing; defaults applied",
                }
            )
            return None
        except StoreCorruptError as exc:
            quarantined = quarantine_corrupt(self.backup_path)
            self.warnings.append(
                {
                    "code": "settings_backup_corrupt",
                    "message": str(exc),
                    "quarantined": quarantined,
                }
            )
            return None
        self.warnings.append(
            {
                "code": "settings_restored_from_backup",
                "message": "settings.json was corrupt; loaded settings.json.bak",
            }
        )
        return payload

    def save(self, settings):
        # type: (object) -> dict
        """Validate and atomically persist settings with a ``.bak`` copy."""
        data, problems = validate_settings(settings, strict=True)
        data["schema_version"] = SCHEMA_VERSION
        atomic_write_json(self.path, data, backup=True)
        self.needs_repair = False
        return data

    def update(self, patch):
        # type: (object) -> dict
        """Merge a validated patch into the current settings."""
        if not isinstance(patch, dict):
            raise StoreError("settings patch must be an object")
        current = self.load()
        merged = dict(current)
        merged.update(patch)
        return self.save(merged)


__all__ = [
    "DEFAULT_SETTINGS",
    "SCHEMA_VERSION",
    "SettingsStore",
    "UNINSTALL_POLICIES",
    "default_settings",
    "validate_settings",
]
