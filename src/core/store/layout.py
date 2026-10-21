# -*- coding: utf-8 -*-
"""Data-root layout and first-run initialisation (requirements 4.2 / 4.5).

The store layer only creates and names files; it never decides what should be
migrated.  Layout mirrors the documented root::

    <data_root>/
      data/
      backup/
      logs/
      config/settings.json
      config/ai-key.bin
      config/rules-learned.json
      config/history.json
      config/migration-jobs/<job_id>.json
      config/locales/

Python 3.8 compatible (decision 0005).
"""

import os
import posixpath
import sys

from .atomic import StoreError, atomic_write_bytes

DATA_DIR = "data"
BACKUP_DIR = "backup"
LOGS_DIR = "logs"
CONFIG_DIR = "config"
CONFIG_SUBDIRS = ("locales", "migration-jobs")
ENV_DATA_ROOT = "DISKWARDEN_DATA_ROOT"
ROOT_PROBE_PREFIX = ".dw-write-test-"


def default_data_root():
    # type: () -> str
    override = os.environ.get(ENV_DATA_ROOT)
    if override:
        # A macOS root is a POSIX path even when this code runs on a Windows
        # test host, so normalize it with the separators it will be used with.
        paths = posixpath if sys.platform == "darwin" else os.path
        return paths.abspath(paths.expanduser(override))
    if sys.platform.startswith("win"):
        return "D:\\DiskWarden"
    if sys.platform == "darwin":
        # The macOS equivalent of %LOCALAPPDATA%: per-user, backed up with the
        # home folder, and not a visible clutter folder in the home directory.
        return os.path.join(
            os.path.expanduser("~"),
            "Library",
            "Application Support",
            "DiskWarden",
        )
    return os.path.join(os.path.expanduser("~"), "DiskWarden")


class DataLayout(object):
    """Names the directories and files below one data root."""

    def __init__(self, root=None):
        # type: (str) -> None
        value = root or default_data_root()
        if not isinstance(value, str) or not value.strip():
            raise StoreError("data root must be a non-empty path")
        self.root = os.path.abspath(os.path.normpath(os.path.expanduser(value.strip())))

    @property
    def data_dir(self):
        # type: () -> str
        return os.path.join(self.root, DATA_DIR)

    @property
    def backup_dir(self):
        # type: () -> str
        return os.path.join(self.root, BACKUP_DIR)

    @property
    def logs_dir(self):
        # type: () -> str
        return os.path.join(self.root, LOGS_DIR)

    @property
    def config_dir(self):
        # type: () -> str
        return os.path.join(self.root, CONFIG_DIR)

    @property
    def locales_dir(self):
        # type: () -> str
        return os.path.join(self.config_dir, "locales")

    @property
    def jobs_dir(self):
        # type: () -> str
        return os.path.join(self.config_dir, "migration-jobs")

    @property
    def settings_path(self):
        # type: () -> str
        return os.path.join(self.config_dir, "settings.json")

    @property
    def learned_path(self):
        # type: () -> str
        return os.path.join(self.config_dir, "rules-learned.json")

    @property
    def ai_key_path(self):
        # type: () -> str
        return os.path.join(self.config_dir, "ai-key.bin")

    @property
    def history_path(self):
        # type: () -> str
        return os.path.join(self.config_dir, "history.json")

    @property
    def directories(self):
        # type: () -> list
        return [
            self.root,
            self.data_dir,
            self.backup_dir,
            self.logs_dir,
            self.config_dir,
            self.jobs_dir,
            self.locales_dir,
        ]

    def ensure(self):
        # type: () -> "DataLayout"
        """Create the root structure; never overwrites existing data."""
        try:
            for directory in self.directories:
                os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            raise StoreError("cannot create data root %s: %s" % (self.root, exc))
        return self

    def is_writable(self):
        # type: () -> bool
        """Probe writability without modifying any user file."""
        probe = os.path.join(
            self.root, "%s%d" % (ROOT_PROBE_PREFIX, os.getpid())
        )
        try:
            self.ensure()
            atomic_write_bytes(probe, b"ok", fsync=False)
            os.unlink(probe)
            return True
        except (OSError, StoreError):
            return False

    def has_migrated_data(self):
        # type: () -> bool
        """True when ``data/`` already holds migrated entries (4.5 guard)."""
        try:
            with os.scandir(self.data_dir) as entries:
                for entry in entries:
                    if not entry.name.startswith("."):
                        return True
        except OSError:
            return False
        return False

    def journal_path(self, job_id):
        # type: (str) -> str
        return os.path.join(self.jobs_dir, "%s.json" % job_id)

    def to_dict(self):
        # type: () -> dict
        return {
            "root": self.root,
            "data_dir": self.data_dir,
            "backup_dir": self.backup_dir,
            "logs_dir": self.logs_dir,
            "config_dir": self.config_dir,
            "jobs_dir": self.jobs_dir,
            "locales_dir": self.locales_dir,
        }


def root_has_migrated_data(root):
    # type: (str) -> bool
    """Pure convenience wrapper used by settings validation."""
    return DataLayout(root).has_migrated_data()


def can_change_root(current_root, new_root):
    # type: (str, str) -> bool
    """Root changes are blocked while ``data/`` is non-empty (4.5).

    Moving to the same root is always allowed.
    """
    current = DataLayout(current_root)
    proposed = DataLayout(new_root)
    if os.path.normcase(current.root) == os.path.normcase(proposed.root):
        return True
    return not current.has_migrated_data()


__all__ = [
    "BACKUP_DIR",
    "CONFIG_DIR",
    "CONFIG_SUBDIRS",
    "DATA_DIR",
    "DataLayout",
    "ENV_DATA_ROOT",
    "LOGS_DIR",
    "can_change_root",
    "default_data_root",
    "root_has_migrated_data",
]
