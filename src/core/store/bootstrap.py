# -*- coding: utf-8 -*-
"""Bootstrap pointer outside the data root (architecture 4.5).

The pointer lives in the OS configuration directory so a missing or
removable data disk does not make the application forget where its settings
are.  Startup order is bootstrap -> writable root -> settings; this module
only reads/writes the pointer and never raises on a missing or damaged file.

Python 3.8 compatible (decision 0005).
"""

import os
import sys
from dataclasses import dataclass

from .atomic import (
    StoreCorruptError,
    atomic_write_json,
    quarantine_corrupt,
    read_json_file,
    utc_now,
)
from .layout import DataLayout

SCHEMA_VERSION = 1
ENV_BOOTSTRAP_PATH = "DISKWARDEN_BOOTSTRAP_PATH"
APP_DIR_NAME = "DiskWarden"
BOOTSTRAP_NAME = "bootstrap.json"


@dataclass(frozen=True)
class Bootstrap:
    schema_version: int
    data_root: str
    updated_at: str

    def to_dict(self):
        # type: () -> dict
        return {
            "schema_version": self.schema_version,
            "data_root": self.data_root,
            "updated_at": self.updated_at,
        }


def default_bootstrap_path():
    # type: () -> str
    override = os.environ.get(ENV_BOOTSTRAP_PATH)
    if override:
        return os.path.abspath(os.path.expanduser(override))
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Local"
        )
        return os.path.join(base, APP_DIR_NAME, BOOTSTRAP_NAME)
    if sys.platform == "darwin":
        return os.path.join(
            os.path.expanduser("~/Library/Application Support"),
            APP_DIR_NAME,
            BOOTSTRAP_NAME,
        )
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP_DIR_NAME, BOOTSTRAP_NAME)


def load_bootstrap(path=None, now=None):
    # type: (str, object) -> object
    """Return ``Bootstrap`` or ``None``; never raises on missing/corrupt data."""
    target = path or default_bootstrap_path()
    try:
        payload = read_json_file(target)
    except FileNotFoundError:
        return None
    except StoreCorruptError:
        quarantine_corrupt(target, now=now)
        return None
    except OSError:
        return None
    if not isinstance(payload, dict):
        quarantine_corrupt(target, now=now)
        return None
    data_root = payload.get("data_root")
    if not isinstance(data_root, str) or not data_root.strip():
        return None
    try:
        root = DataLayout(data_root).root
    except Exception:
        return None
    return Bootstrap(
        schema_version=_schema_version(payload.get("schema_version")),
        data_root=root,
        updated_at=str(payload.get("updated_at") or ""),
    )


def save_bootstrap(data_root, path=None, now=None):
    # type: (str, str, object) -> Bootstrap
    """Atomically point the bootstrap file at ``data_root``."""
    root = DataLayout(data_root).root
    record = Bootstrap(
        schema_version=SCHEMA_VERSION,
        data_root=root,
        updated_at=utc_now(now),
    )
    atomic_write_json(path or default_bootstrap_path(), record.to_dict())
    return record


def _schema_version(value):
    # type: (object) -> int
    if isinstance(value, bool):
        return SCHEMA_VERSION
    if isinstance(value, int) and value > 0:
        return value
    return SCHEMA_VERSION


__all__ = [
    "APP_DIR_NAME",
    "Bootstrap",
    "ENV_BOOTSTRAP_PATH",
    "SCHEMA_VERSION",
    "default_bootstrap_path",
    "load_bootstrap",
    "save_bootstrap",
]
