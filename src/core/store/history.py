# -*- coding: utf-8 -*-
"""History persistence (requirements 2.7 / AC-HIS-01/02).

History is an append-only list of migration and rollback records.  The file is
rewritten atomically on every append so a crash leaves either the previous
complete list or the new complete list; the last incomplete record can never
be observed (AC-STORE-02).  When the list exceeds ``max_records`` the oldest
records move to a monthly archive instead of being deleted.

Cumulative "released" bytes are computed from successful migrations that have
no rollback record; when history is missing or damaged the caller is told the
statistic is incomplete instead of receiving a fabricated number.

Python 3.8 compatible (decision 0005).
"""

import datetime
import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .atomic import (
    StoreCorruptError,
    StoreError,
    atomic_write_json,
    quarantine_corrupt,
    read_json_file,
    utc_now,
)

SCHEMA_VERSION = 1
DEFAULT_MAX_RECORDS = 5000

TYPE_MIGRATION = "migration"
TYPE_ROLLBACK = "rollback"
TYPE_VALUES = (TYPE_MIGRATION, TYPE_ROLLBACK)

RESULT_SUCCESS = "success"
RESULT_FAILURE = "failure"
RESULT_CANCELLED = "cancelled"
RESULT_ROLLED_BACK = "rolled_back"
RESULT_VALUES = (
    RESULT_SUCCESS,
    RESULT_FAILURE,
    RESULT_CANCELLED,
    RESULT_ROLLED_BACK,
)

FIELDS = (
    "time",
    "type",
    "job_id",
    "tool",
    "item",
    "source",
    "target",
    "bytes",
    "result",
    "stage",
    "reason",
)


@dataclass(frozen=True)
class HistoryRecord(object):
    """One append-only history line (AC-HIS-01)."""

    time: str
    type: str
    job_id: str = ""
    tool: str = ""
    item: str = ""
    source: str = ""
    target: str = ""
    bytes: int = 0
    result: str = RESULT_SUCCESS
    stage: str = ""
    reason: str = ""

    def to_dict(self):
        # type: () -> Dict[str, object]
        data = {
            "time": self.time,
            "type": self.type,
            "job_id": self.job_id,
            "tool": self.tool,
            "item": self.item,
            "source": self.source,
            "target": self.target,
            "bytes": self.bytes,
            "result": self.result,
            "stage": self.stage,
            "reason": self.reason,
        }
        return data


@dataclass(frozen=True)
class ReleasedStats(object):
    """Cumulative released bytes and whether the number is trustworthy."""

    total_bytes: int = 0
    migrations: int = 0
    rolled_back: int = 0
    complete: bool = True
    warnings: Tuple[str, ...] = ()

    def to_dict(self):
        # type: () -> Dict[str, object]
        return {
            "total_bytes": self.total_bytes,
            "migrations": self.migrations,
            "rolled_back": self.rolled_back,
            "complete": self.complete,
            "warnings": list(self.warnings),
        }


def _text(value):
    # type: (object) -> str
    return value if isinstance(value, str) else ""


def _non_negative_int(value):
    # type: (object) -> Optional[int]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def normalize_record(raw, now=None):
    # type: (object, object) -> Optional[HistoryRecord]
    """Return a validated record or ``None`` when the line is unusable."""
    if not isinstance(raw, dict):
        return None
    record_type = _text(raw.get("type"))
    if record_type not in TYPE_VALUES:
        return None
    result = _text(raw.get("result"))
    if result not in RESULT_VALUES:
        return None
    size = _non_negative_int(raw.get("bytes"))
    if size is None:
        return None
    timestamp = _text(raw.get("time")) or utc_now(now)
    return HistoryRecord(
        time=timestamp,
        type=record_type,
        job_id=_text(raw.get("job_id")),
        tool=_text(raw.get("tool")),
        item=_text(raw.get("item")),
        source=_text(raw.get("source")),
        target=_text(raw.get("target")),
        bytes=size,
        result=result,
        stage=_text(raw.get("stage")),
        reason=_text(raw.get("reason")),
    )


class HistoryStore(object):
    """``<config>/history.json`` with append and archive semantics."""

    def __init__(self, path, max_records=DEFAULT_MAX_RECORDS):
        # type: (str, int) -> None
        if not path:
            raise StoreError("history path is required")
        self.path = os.path.abspath(os.path.normpath(path))
        self.max_records = max(0, int(max_records))
        self.warnings = []  # type: List[Dict[str, object]]
        self.complete = True
        self.records = []  # type: List[HistoryRecord]

    def load(self):
        # type: () -> List[HistoryRecord]
        """Read and validate the file; never raises for a damaged file."""
        self.warnings = []
        self.complete = True
        self.records = []
        try:
            payload = read_json_file(self.path)
        except FileNotFoundError:
            self.complete = False
            self.warnings.append(
                {"code": "history_missing", "message": "history file is missing"}
            )
            return []
        except StoreCorruptError as exc:
            self.complete = False
            quarantined = quarantine_corrupt(self.path)
            self.warnings.append(
                {
                    "code": "history_corrupt",
                    "message": str(exc),
                    "quarantined": quarantined,
                }
            )
            return []

        raw_records = self._records_from(payload)
        if raw_records is None:
            self.complete = False
            quarantined = quarantine_corrupt(self.path)
            self.warnings.append(
                {
                    "code": "history_corrupt",
                    "message": "history must be a list or an object with records",
                    "quarantined": quarantined,
                }
            )
            return []

        invalid = 0
        for raw in raw_records:
            record = normalize_record(raw)
            if record is None:
                invalid += 1
                continue
            self.records.append(record)
        if invalid:
            self.complete = False
            self.warnings.append(
                {
                    "code": "history_record_invalid",
                    "message": "%d history record(s) were unusable" % invalid,
                }
            )
        return list(self.records)

    @staticmethod
    def _records_from(payload):
        # type: (object) -> Optional[List[object]]
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            records = payload.get("records")
            if isinstance(records, list):
                return records
        return None

    def save(self, records):
        # type: (List[object]) -> List[HistoryRecord]
        """Atomically write ``records`` (already-normalised or raw dicts)."""
        normalised = []  # type: List[HistoryRecord]
        for raw in records:
            record = raw if isinstance(raw, HistoryRecord) else normalize_record(raw)
            if record is None:
                raise StoreError("history record is invalid: %r" % (raw,))
            normalised.append(record)
        self._write(normalised)
        self.records = normalised
        return list(normalised)

    def append(self, record):
        # type: (object) -> HistoryRecord
        """Append one validated record and return its normalised form."""
        if isinstance(record, HistoryRecord):
            normal = record
        else:
            normal = normalize_record(record)
        if normal is None:
            raise StoreError("history record is invalid: %r" % (record,))
        current = self.load()
        current.append(normal)
        kept = self._trim(current)
        self._write(kept)
        self.records = kept
        return normal

    def record_migration(
        self,
        tool,
        item,
        source,
        target,
        size,
        result=RESULT_SUCCESS,
        job_id="",
        stage="",
        reason="",
        now=None,
    ):
        # type: (str, str, str, str, int, str, str, str, str, object) -> HistoryRecord
        return self.append(
            HistoryRecord(
                time=utc_now(now),
                type=TYPE_MIGRATION,
                job_id=job_id or "",
                tool=tool or "",
                item=item or "",
                source=source or "",
                target=target or "",
                bytes=size,
                result=result,
                stage=stage or "",
                reason=reason or "",
            )
        )

    def record_rollback(
        self,
        tool,
        item,
        source,
        target,
        size,
        result=RESULT_ROLLED_BACK,
        job_id="",
        stage="",
        reason="",
        now=None,
    ):
        # type: (str, str, str, str, int, str, str, str, str, object) -> HistoryRecord
        return self.append(
            HistoryRecord(
                time=utc_now(now),
                type=TYPE_ROLLBACK,
                job_id=job_id or "",
                tool=tool or "",
                item=item or "",
                source=source or "",
                target=target or "",
                bytes=size,
                result=result,
                stage=stage or "",
                reason=reason or "",
            )
        )

    def find(self, job_id, record_type=None):
        # type: (str, str) -> List[HistoryRecord]
        records = self.load()
        return [
            record
            for record in records
            if record.job_id == job_id
            and (record_type is None or record.type == record_type)
        ]

    def was_rolled_back(self, job_id):
        # type: (str) -> bool
        return any(
            record.type == TYPE_ROLLBACK
            and record.result in (RESULT_ROLLED_BACK, RESULT_SUCCESS)
            for record in self.find(job_id)
        )

    def released_stats(self, records=None):
        # type: (Optional[List[HistoryRecord]]) -> ReleasedStats
        """Released bytes for successful migrations that were not rolled back."""
        if records is None:
            records = self.load()
            complete = self.complete
            warnings = tuple(
                str(item.get("message", ""))
                for item in self.warnings
                if item.get("code") != "history_missing"
            )
        else:
            complete = True
            warnings = ()

        migration_totals = {}  # type: Dict[object, int]
        migration_count = 0
        for record in records:
            if record.type != TYPE_MIGRATION or record.result != RESULT_SUCCESS:
                continue
            migration_count += 1
            key = record.job_id or ("%s\n%s" % (record.tool, record.item))
            migration_totals[key] = migration_totals.get(key, 0) + record.bytes

        rolled_back = set()
        for record in records:
            if (
                record.type == TYPE_ROLLBACK
                and record.result in (RESULT_ROLLED_BACK, RESULT_SUCCESS)
                and record.job_id
            ):
                rolled_back.add(record.job_id)

        total = sum(
            size
            for key, size in migration_totals.items()
            if key not in rolled_back
        )
        return ReleasedStats(
            total_bytes=total,
            migrations=migration_count,
            rolled_back=len(rolled_back),
            complete=complete,
            warnings=warnings,
        )

    def _trim(self, records):
        # type: (List[HistoryRecord]) -> List[HistoryRecord]
        if self.max_records <= 0 or len(records) <= self.max_records:
            return records
        overflow = records[: len(records) - self.max_records]
        kept = records[len(records) - self.max_records:]
        try:
            self._archive(overflow)
        except StoreError as exc:
            # Never drop history just because the archive could not be
            # written; keep everything and report the problem instead.
            self.warnings.append(
                {"code": "history_archive_failed", "message": str(exc)}
            )
            return records
        return kept

    def _archive(self, records):
        # type: (List[HistoryRecord]) -> str
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")
        target = os.path.join(
            os.path.dirname(self.path), "history-%s.json" % stamp
        )
        existing = []  # type: List[HistoryRecord]
        try:
            payload = read_json_file(target)
        except FileNotFoundError:
            payload = None
        except StoreCorruptError as exc:
            raise StoreError("history archive is corrupt: %s" % exc)
        if payload is not None:
            raw_records = self._records_from(payload)
            if raw_records is None:
                raise StoreError("history archive has an unexpected shape")
            for raw in raw_records:
                record = normalize_record(raw)
                if record is not None:
                    existing.append(record)
        combined = [record.to_dict() for record in existing + records]
        atomic_write_json(
            target,
            {
                "schema_version": SCHEMA_VERSION,
                "updated_at": utc_now(),
                "records": combined,
            },
        )
        return target

    def _write(self, records):
        # type: (List[HistoryRecord]) -> None
        atomic_write_json(
            self.path,
            {
                "schema_version": SCHEMA_VERSION,
                "updated_at": utc_now(),
                "records": [record.to_dict() for record in records],
            },
        )


__all__ = [
    "DEFAULT_MAX_RECORDS",
    "FIELDS",
    "HistoryRecord",
    "HistoryStore",
    "RESULT_CANCELLED",
    "RESULT_FAILURE",
    "RESULT_ROLLED_BACK",
    "RESULT_SUCCESS",
    "RESULT_VALUES",
    "ReleasedStats",
    "SCHEMA_VERSION",
    "TYPE_MIGRATION",
    "TYPE_ROLLBACK",
    "TYPE_VALUES",
    "normalize_record",
]
