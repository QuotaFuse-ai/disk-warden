# -*- coding: utf-8 -*-
"""Local persistence layer (T11): layout, atomic JSON, settings, history,
learned rules, the migration/rollback journal and JSONL logs.

Nothing here makes migration decisions; the rollback and migration engines
consume these stores.  Every file is written with the same atomic
temp-file + ``os.replace`` primitive, so a crash cannot leave a half-written
JSON document (AC-STORE-02).

Python 3.8 compatible (decision 0005).
"""

from .atomic import (
    StoreCorruptError,
    StoreError,
    atomic_write_bytes,
    atomic_write_json,
    dump_json_bytes,
    is_temp_name,
    load_json,
    quarantine_corrupt,
    read_json_file,
    temp_path,
    utc_now,
)
from .bootstrap import (
    Bootstrap,
    default_bootstrap_path,
    load_bootstrap,
    save_bootstrap,
)
from .history import (
    DEFAULT_MAX_RECORDS,
    HistoryRecord,
    HistoryStore,
    ReleasedStats,
    RESULT_CANCELLED,
    RESULT_FAILURE,
    RESULT_ROLLED_BACK,
    RESULT_SUCCESS,
    TYPE_MIGRATION,
    TYPE_ROLLBACK,
)
from .journal import (
    JournalEntry,
    JournalStore,
    entry_from_dict,
    validate_job_id,
)
from .layout import (
    DataLayout,
    can_change_root,
    default_data_root,
    root_has_migrated_data,
)
from .learned import (
    LEARNED_SAFETY_VALUES,
    LearnedRuleError,
    LearnedRulesStore,
    SAFETY_GREEN,
    SAFETY_RED,
)
from .logs import JsonlLogger, mask_path, mask_text
from .settings import (
    DEFAULT_SETTINGS,
    SettingsStore,
    UNINSTALL_POLICIES,
    default_settings,
    validate_settings,
)

__all__ = [
    "Bootstrap",
    "DEFAULT_MAX_RECORDS",
    "DEFAULT_SETTINGS",
    "DataLayout",
    "HistoryRecord",
    "HistoryStore",
    "JournalEntry",
    "JournalStore",
    "JsonlLogger",
    "LEARNED_SAFETY_VALUES",
    "LearnedRuleError",
    "LearnedRulesStore",
    "RESULT_CANCELLED",
    "RESULT_FAILURE",
    "RESULT_ROLLED_BACK",
    "RESULT_SUCCESS",
    "ReleasedStats",
    "SAFETY_GREEN",
    "SAFETY_RED",
    "SettingsStore",
    "StoreCorruptError",
    "StoreError",
    "TYPE_MIGRATION",
    "TYPE_ROLLBACK",
    "UNINSTALL_POLICIES",
    "atomic_write_bytes",
    "atomic_write_json",
    "can_change_root",
    "default_bootstrap_path",
    "default_data_root",
    "default_settings",
    "dump_json_bytes",
    "entry_from_dict",
    "is_temp_name",
    "load_bootstrap",
    "load_json",
    "mask_path",
    "mask_text",
    "quarantine_corrupt",
    "read_json_file",
    "root_has_migrated_data",
    "save_bootstrap",
    "temp_path",
    "utc_now",
    "validate_job_id",
    "validate_settings",
]
