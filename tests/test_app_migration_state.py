# -*- coding: utf-8 -*-
"""Migration state survives scans and restarts (QUOT-32).

A migrated directory is a junction, so a fresh scan must restore the
``migrated_job_id`` from history instead of reporting "source is a reparse
point".  Every fixture lives in a temporary directory; no real user folder is
read or moved.
"""

import os
import subprocess
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from src.app import service as service_module
from src.app.errors import BridgeError
from src.app.service import AppService, _normalize_source_path
from src.core.store.journal import (
    STATE_DONE,
    JournalStore,
)
from src.core.store.history import RESULT_SUCCESS

from tests.app_fakes import (
    CATALOG_PATH,
    FOUR_GIB,
    RULES_PATH,
    make_item,
    make_service,
    set_snapshot,
)

JOB_ID = "0123456789abcdef"
TOOL_ID = "trae"


class _Measured(object):
    """Stand-in for one measured scan item."""

    def __init__(self, source_path, is_reparse=True, access_errors=0):
        self.tool_id = TOOL_ID
        self.item_id = TOOL_ID + "-cache"
        self.source_path = source_path
        self.rel_path = os.path.basename(source_path.rstrip("\\/"))
        self.kind = "dir"
        self.bytes = 4096
        self.file_count = 4
        self.is_reparse = is_reparse
        self.access_errors = access_errors
        self.mtime_max = ""


class _FakeTool(object):
    def __init__(self, tool_id):
        self.id = tool_id
        self.name = tool_id

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.id,
            "kind": "ide",
            "status": "present",
            "root": "",
        }


def _make_service_at(case, data_root):
    service = AppService(
        data_root=data_root,
        catalog_path=CATALOG_PATH,
        rules_path=RULES_PATH,
        environ={},
        process_provider=lambda: [],
        free_space_provider=lambda path: FOUR_GIB,
    )
    case.addCleanup(service.close)
    return service


def _make_junction(case, link, target):
    if os.name != "nt":
        case.skipTest("junctions are only available on Windows")
    os.makedirs(target)
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", link, target],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        case.skipTest(
            "could not create a junction: %s"
            % completed.stderr.decode("mbcs", "replace").strip()
        )
    # rmdir removes the junction itself; the target's contents stay intact.
    case.addCleanup(lambda: os.rmdir(link) if os.path.isdir(link) else None)


def _write_success(
    service, source, target, job_id=JOB_ID, journal_state=STATE_DONE
):
    """Persist one successful migration the way the service does."""
    service.history.record_migration(
        tool=TOOL_ID,
        item=os.path.basename(source.rstrip("\\/")) or source,
        source=source,
        target=target,
        size=4096,
        result=RESULT_SUCCESS,
        job_id=job_id,
        stage="record",
    )
    if journal_state is not None:
        store = JournalStore(service.layout.jobs_dir)
        store.begin(
            job_id,
            tool_id=TOOL_ID,
            source=source,
            final=target,
        )
        if journal_state != "PLANNED":
            store.set_result(job_id, journal_state, payload={"ok": True})


def _run_scan(service, measured_items):
    """Run ``_scan_worker`` with discovery/measurement stubbed out."""
    catalog = SimpleNamespace(tools=[_FakeTool(TOOL_ID)])
    discovery = SimpleNamespace(tools=[_FakeTool(TOOL_ID)], warnings=[])
    report = SimpleNamespace(items=list(measured_items), warnings=[])

    def emit(*args, **kwargs):
        return None

    with mock.patch.object(
        service_module, "load_catalog", return_value=catalog
    ), mock.patch.object(
        service_module, "discover", return_value=discovery
    ), mock.patch.object(
        service_module, "targets_from_discovery", return_value=[]
    ), mock.patch.object(
        service_module, "measure", return_value=report
    ), mock.patch.object(
        service_module, "detect_processes", return_value={}
    ):
        service._scan_worker(
            SimpleNamespace(job_id="scanjob"), threading.Event(), emit
        )
    return service.scan_result()


class MigratedScanStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_root = os.path.join(self.tmp.name, "data")
        self.target = os.path.join(self.tmp.name, "target", "trae-cache")
        self.source = os.path.join(self.tmp.name, "source", "Trae", "Cache")
        os.makedirs(os.path.dirname(self.source))
        _make_junction(self, self.source, self.target)

    def test_junction_is_reported_as_migrated_after_scan_and_restart(self):
        first = _make_service_at(self, self.data_root)
        _write_success(first, self.source, self.target)
        first.close()

        restarted = _make_service_at(self, self.data_root)
        snapshot = _run_scan(restarted, [_Measured(self.source)])

        self.assertEqual(len(snapshot["items"]), 1)
        item = snapshot["items"][0]
        self.assertEqual(item["migrated_job_id"], JOB_ID)
        self.assertEqual(item["blocked_code"], "ALREADY_MIGRATED")
        self.assertFalse(item["can_migrate"])
        self.assertNotIn("重解析点", item["blocked_reason"])
        self.assertEqual(item["target_path"], self.target)
        self.assertTrue(item["target_exists"])

    def test_plain_reparse_item_still_gets_the_reparse_block(self):
        service = _make_service_at(self, self.data_root)
        snapshot = _run_scan(service, [_Measured(self.source)])

        item = snapshot["items"][0]
        self.assertEqual(item["migrated_job_id"], "")
        self.assertEqual(item["blocked_code"], "BLOCKED_RED_PATH")
        self.assertIn("重解析点", item["blocked_reason"])

    def test_case_and_separator_differences_still_match(self):
        if os.name != "nt":
            self.skipTest("path case folding is Windows-only")
        service = _make_service_at(self, self.data_root)
        recorded = self.source.upper().replace("/", "\\")
        _write_success(service, recorded, self.target)
        measured_path = self.source.replace("\\", "/").lower()

        snapshot = _run_scan(service, [_Measured(measured_path)])
        self.assertEqual(snapshot["items"][0]["migrated_job_id"], JOB_ID)

    def test_rollback_record_cancels_migrated_state(self):
        service = _make_service_at(self, self.data_root)
        _write_success(service, self.source, self.target)
        service.history.record_rollback(
            tool=TOOL_ID,
            item="Cache",
            source=self.source,
            target=self.target,
            size=4096,
            job_id=JOB_ID,
        )

        snapshot = _run_scan(service, [_Measured(self.source)])
        self.assertEqual(snapshot["items"][0]["migrated_job_id"], "")
        self.assertEqual(service._migrated_index(), {})

    def test_unfinished_journal_is_not_treated_as_migrated(self):
        service = _make_service_at(self, self.data_root)
        _write_success(
            service, self.source, self.target, journal_state="PLANNED"
        )

        self.assertEqual(service._migrated_index(), {})

    def test_missing_journal_falls_back_to_history(self):
        service = _make_service_at(self, self.data_root)
        _write_success(service, self.source, self.target, journal_state=None)

        index = service._migrated_index()
        self.assertIn(_normalize_source_path(self.source), index)


class MigratedSnapshotBehaviourTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service(self)

    def test_plan_preview_rejects_already_migrated_items(self):
        item = make_item(
            migrated_job_id=JOB_ID,
            can_migrate=False,
            blocked_code="ALREADY_MIGRATED",
            blocked_reason="已迁移到目标盘，不能重复迁移",
        )
        set_snapshot(self.service, [item])

        with self.assertRaises(BridgeError) as ctx:
            self.service.plan_preview([item["item_id"]])
        self.assertEqual(ctx.exception.code, "ALREADY_MIGRATED")
        detail = ctx.exception.detail.get("items") or []
        self.assertEqual(detail[0]["job_id"], JOB_ID)

    def test_rollback_clears_migrated_state_for_a_retry(self):
        item = make_item(
            migrated_job_id=JOB_ID,
            can_migrate=False,
            blocked_code="ALREADY_MIGRATED",
            blocked_reason="已迁移到目标盘，不能重复迁移",
        )
        set_snapshot(self.service, [item])

        self.service._apply_rollback_to_snapshot(
            JOB_ID, item["tool_id"], item["source_path"]
        )

        self.assertEqual(item["migrated_job_id"], "")
        self.assertEqual(item["blocked_code"], "")
        self.assertEqual(item["blocked_reason"], "")
        self.assertTrue(item["can_migrate"])

    def test_rollback_keeps_green_rows_selectable_without_confirmation(self):
        item = make_item(
            safety="green",
            migrated_job_id=JOB_ID,
            can_migrate=False,
            blocked_code="ALREADY_MIGRATED",
            blocked_reason="已迁移到目标盘，不能重复迁移",
            requires_confirm=False,
        )
        set_snapshot(self.service, [item])

        self.service._apply_rollback_to_snapshot(
            JOB_ID, item["tool_id"], item["source_path"]
        )

        self.assertEqual(item["safety"], "green")
        self.assertFalse(item["requires_confirm"])
        self.assertTrue(item["can_migrate"])

    def test_rollback_keeps_yellow_rows_reachable_behind_confirmation(self):
        item = make_item(
            safety="yellow",
            migrated_job_id=JOB_ID,
            can_migrate=False,
            requires_confirm=True,
            blocked_code="ALREADY_MIGRATED",
            blocked_reason="已迁移到目标盘，不能重复迁移",
        )
        set_snapshot(self.service, [item])

        self.service._apply_rollback_to_snapshot(
            JOB_ID, item["tool_id"], item["source_path"]
        )

        self.assertEqual(item["safety"], "yellow")
        self.assertTrue(item["requires_confirm"])
        self.assertTrue(item["can_migrate"])

    def test_rollback_never_unlocks_a_red_path(self):
        item = make_item(
            safety="red",
            migrated_job_id=JOB_ID,
            can_migrate=False,
            blocked_code="ALREADY_MIGRATED",
            blocked_reason="已迁移到目标盘，不能重复迁移",
        )
        set_snapshot(self.service, [item])

        self.service._apply_rollback_to_snapshot(
            JOB_ID, item["tool_id"], item["source_path"]
        )

        self.assertEqual(item["safety"], "red")
        self.assertFalse(item["can_migrate"])


if __name__ == "__main__":
    unittest.main()
