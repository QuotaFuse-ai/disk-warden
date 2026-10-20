# -*- coding: utf-8 -*-
"""Unit tests for the one-click rollback engine (T11 / architecture 6.7).

Junctions are simulated in memory by ``FakeOps``; the real filesystem holds
the source, target and restore trees so every verification and ownership
check runs against real bytes.  The transfer primitive is injected, so the
tests never depend on robocopy and never touch real user data.
"""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.migrate import paths as migrate_paths  # noqa: E402
from src.core.rollback import engine as rollback_engine  # noqa: E402
from src.core.rollback.engine import precheck_rollback, rollback  # noqa: E402
from src.core.rollback.models import (  # noqa: E402
    ERROR_COPY,
    ERROR_INSUFFICIENT_SPACE,
    ERROR_INVALID_INPUT,
    ERROR_PROCESS_BUSY,
    ERROR_RECOVERY,
    ERROR_STATE,
    ERROR_TARGET_UNAVAILABLE,
    ERROR_VERIFY,
    MODE_ROLLBACK,
    SOURCE_KIND_JUNCTION,
    STATE_ALREADY_ROLLED_BACK,
    STATE_RESIDUAL_COPY,
    STATE_ROLLBACK_FAILED,
    STATE_ROLLED_BACK,
    STATE_STATE_ABNORMAL,
    RollbackRequest,
)
from src.core.rules import item_id_for  # noqa: E402
from src.core.store.history import (  # noqa: E402
    RESULT_ROLLED_BACK,
    TYPE_ROLLBACK,
    HistoryStore,
)
from src.core.store.journal import (  # noqa: E402
    STATE_DONE,
    TYPE_MIGRATION,
    TYPE_ROLLBACK as JOURNAL_TYPE_ROLLBACK,
    JournalStore,
)
from src.core.store.learned import SAFETY_RED, LearnedRulesStore  # noqa: E402
from tests.migrate_fakes import (  # noqa: E402
    JOB8,
    JOB_ID,
    FILE_PAYLOADS,
    FakeOps,
    MigrationFixture,
    Process,
    expected_snapshot,
    snapshot,
)


class _LinkOnlyOps(FakeOps):
    """A source that is a link but not a junction (must never be removed)."""

    def is_junction(self, path):
        return False


class RollbackEngineTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory(prefix="dw-t11-rollback-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.fixture = MigrationFixture(self.root)
        self.source = self.fixture.source
        self.source_parent = self.fixture.source_parent
        self.final = self.fixture.final
        self.ops = FakeOps()
        self.config_dir = self.root / "config"
        self.jobs_dir = self.config_dir / "migration-jobs"
        self.jobs_dir.mkdir(parents=True)
        self.history_path = self.config_dir / "history.json"
        self.learned_path = self.config_dir / "rules-learned.json"
        self.backup_dir = (
            self.root / "data" / "backup" / ("2026-10-19_120000_demo_" + JOB8)
        )
        JournalStore(str(self.jobs_dir)).begin(
            JOB_ID,
            entry_type=TYPE_MIGRATION,
            tool_id="demo",
            source=str(self.source),
            final=str(self.final),
            backup_dir=str(self.backup_dir),
            state=STATE_DONE,
        )

    # -- helpers ---------------------------------------------------------

    def _make_migrated(self, owned=True):
        """Create the target, move the source aside and register the junction."""
        self.fixture.make_final(owned=owned)
        self.fixture.move_source_to_tombstone()
        self.ops.create_link(str(self.source), str(self.final))

    def _request(self, **overrides):
        values = dict(
            job_id=JOB_ID,
            tool_id="demo",
            source_path=str(self.source),
            final_path=str(self.final),
            backup_dir=str(self.backup_dir),
        )
        values.update(overrides)
        return RollbackRequest(**values)

    def _copier(self, exit_code=0, copy=True, mutate=None):
        def transfer(source, staging, log_path, cancel_token=None):
            if copy:
                shutil.copytree(str(source), str(staging))
                if mutate is not None:
                    mutate(Path(staging))
            return exit_code

        return transfer

    def _run(self, request=None, **overrides):
        overrides.setdefault("transfer", self._copier())
        overrides.setdefault("ops", self.ops)
        overrides.setdefault("process_provider", lambda: [])
        overrides.setdefault("jobs_dir", str(self.jobs_dir))
        overrides.setdefault("history_path", str(self.history_path))
        overrides.setdefault("learned_path", str(self.learned_path))
        overrides.setdefault("sample_seconds", 0.05)
        return rollback(request or self._request(), **overrides)

    def _make_config_backup(self, rel="settings.json", payload=b'{"theme": "dark"}'):
        target = self.backup_dir / "files" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        manifest = {
            "schema_version": 1,
            "tool_id": "demo",
            "job_id": JOB_ID,
            "source_root": str(self.source_parent),
            "entries": [
                {
                    "rel_path": rel,
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            ],
        }
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        (self.backup_dir / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        return payload

    def _restore_slot(self):
        return migrate_paths.restore_path(str(self.source), JOB_ID)

    # -- success path ----------------------------------------------------

    def test_success_restores_source_deletes_target_and_records_history(self):
        self._make_migrated()
        payload = self._make_config_backup()

        result = self._run()

        self.assertTrue(result.ok)
        self.assertEqual(result.state, STATE_ROLLED_BACK)
        self.assertTrue(result.link_removed)
        self.assertTrue(result.source_restored)
        self.assertTrue(result.target_removed)
        self.assertTrue(result.data_kept)
        self.assertFalse(result.residual_copy)
        self.assertFalse(result.relinked)

        self.assertTrue(self.source.is_dir())
        self.assertEqual(snapshot(self.source), expected_snapshot())
        self.assertFalse(self.final.exists())
        self.assertFalse(os.path.lexists(self._restore_slot()))
        self.assertEqual(
            (self.source_parent / "settings.json").read_bytes(), payload
        )
        self.assertEqual(result.config.status, "restored")
        self.assertTrue(result.history_recorded)
        self.assertTrue(result.learned_updated)

        records = HistoryStore(str(self.history_path)).find(
            JOB_ID, TYPE_ROLLBACK
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].result, RESULT_ROLLED_BACK)
        self.assertGreater(records[0].bytes, 0)

        rules = LearnedRulesStore(str(self.learned_path)).load()
        key = item_id_for("demo", source_path=str(self.source))
        self.assertEqual(rules[key]["safety"], SAFETY_RED)
        self.assertEqual(rules[key]["rule_id"], "rollback")

        entry = JournalStore(str(self.jobs_dir)).load(
            JOB_ID, JOURNAL_TYPE_ROLLBACK
        )
        self.assertEqual(entry.state, STATE_ROLLED_BACK)

    def test_config_is_restored_when_the_journal_records_the_backup_root(self):
        """T12-F: an older journal stored the backup root, not the job dir."""
        self._make_migrated()
        payload = self._make_config_backup()
        backup_root = self.backup_dir.parent
        JournalStore(str(self.jobs_dir)).update(
            JOB_ID, backup_dir=str(backup_root)
        )

        result = self._run(self._request(backup_dir=str(backup_root)))

        self.assertTrue(result.ok)
        self.assertEqual(result.state, STATE_ROLLED_BACK)
        self.assertEqual(result.config.status, "restored")
        self.assertTrue(result.config.ok)
        self.assertEqual(
            result.config.manifest_path,
            str(self.backup_dir / "manifest.json"),
        )
        self.assertEqual(
            result.config.checksums["settings.json"],
            hashlib.sha256(payload).hexdigest(),
        )
        self.assertEqual(
            (self.source_parent / "settings.json").read_bytes(), payload
        )

    def test_config_is_skipped_when_the_migration_changed_no_config(self):
        self._make_migrated()
        JournalStore(str(self.jobs_dir)).update(JOB_ID, backup_dir="")

        result = self._run(self._request(backup_dir=""))

        self.assertTrue(result.ok)
        self.assertEqual(result.state, STATE_ROLLED_BACK)
        self.assertTrue(result.data_rolled_back)
        self.assertEqual(result.config.status, "skipped")
        self.assertTrue(result.config.ok)
        self.assertEqual(result.config_status, "skipped")
        self.assertFalse(result.warnings)

    def test_precheck_reports_rollback_mode_for_a_matching_junction(self):
        self._make_migrated()

        precheck = precheck_rollback(
            self._request(),
            ops=self.ops,
            process_provider=lambda: [],
            jobs_dir=str(self.jobs_dir),
        )

        self.assertTrue(precheck.ok)
        self.assertTrue(precheck.can_proceed)
        self.assertEqual(precheck.mode, MODE_ROLLBACK)
        self.assertEqual(precheck.source_kind, SOURCE_KIND_JUNCTION)
        self.assertTrue(precheck.source_link_matches)
        self.assertTrue(precheck.target_owned)
        self.assertEqual(precheck.target_files, len(FILE_PAYLOADS) + 1)
        self.assertGreater(precheck.target_bytes, 0)

    # -- blocked preconditions -------------------------------------------

    def test_missing_target_leaves_the_junction_in_place(self):
        self._make_migrated()
        shutil.rmtree(str(self.final))

        result = self._run()

        self.assertFalse(result.ok)
        self.assertTrue(result.blocked)
        self.assertEqual(result.error_code, ERROR_TARGET_UNAVAILABLE)
        self.assertEqual(result.state, STATE_ROLLBACK_FAILED)
        self.assertFalse(result.link_removed)
        self.assertTrue(result.data_kept)
        self.assertTrue(self.ops.is_junction(str(self.source)))
        self.assertIsNone(
            JournalStore(str(self.jobs_dir)).load(
                JOB_ID, JOURNAL_TYPE_ROLLBACK
            )
        )

    def test_junction_pointing_elsewhere_is_state_abnormal(self):
        self.fixture.make_final(owned=True)
        self.fixture.move_source_to_tombstone()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        self.ops.create_link(str(self.source), str(elsewhere))

        result = self._run()

        self.assertTrue(result.blocked)
        self.assertEqual(result.error_code, ERROR_STATE)
        self.assertEqual(result.state, STATE_STATE_ABNORMAL)
        self.assertTrue(self.ops.is_junction(str(self.source)))
        self.assertTrue(self.final.exists())

    def test_non_junction_link_is_never_removed(self):
        self.fixture.make_final(owned=True)
        self.fixture.move_source_to_tombstone()
        ops = _LinkOnlyOps()
        ops.create_link(str(self.source), str(self.final))

        result = self._run(ops=ops)

        self.assertTrue(result.blocked)
        self.assertEqual(result.error_code, ERROR_STATE)
        self.assertEqual(result.state, STATE_STATE_ABNORMAL)
        self.assertTrue(ops.is_link(str(self.source)))
        self.assertTrue(self.final.exists())

    def test_running_process_blocks_before_any_change(self):
        self._make_migrated()

        result = self._run(
            self._request(process_names=("Cursor.exe",)),
            process_provider=lambda: [Process(4321, "Cursor.exe")],
        )

        self.assertTrue(result.blocked)
        self.assertEqual(result.error_code, ERROR_PROCESS_BUSY)
        self.assertTrue(result.data_kept)
        self.assertTrue(self.ops.is_junction(str(self.source)))
        self.assertTrue(self.final.exists())

    def test_insufficient_space_blocks_before_any_change(self):
        self._make_migrated()

        result = self._run(free_space_provider=lambda directory: 0)

        self.assertTrue(result.blocked)
        self.assertEqual(result.error_code, ERROR_INSUFFICIENT_SPACE)
        self.assertTrue(self.ops.is_junction(str(self.source)))
        self.assertTrue(self.final.exists())

    def test_invalid_request_touches_nothing(self):
        request = RollbackRequest(
            job_id="not-hex",
            tool_id="demo",
            source_path=str(self.source),
            final_path=str(self.final),
        )

        result = self._run(request)

        self.assertTrue(result.blocked)
        self.assertEqual(result.error_code, ERROR_INVALID_INPUT)
        self.assertTrue(result.data_kept)
        self.assertTrue(self.source.exists())

    def test_migration_journal_mismatch_blocks(self):
        self._make_migrated()

        result = self._run(self._request(tool_id="other"))

        self.assertTrue(result.blocked)
        self.assertEqual(result.error_code, ERROR_INVALID_INPUT)
        self.assertTrue(self.ops.is_junction(str(self.source)))
        self.assertTrue(self.final.exists())

    def test_missing_migration_journal_requires_recovery(self):
        self._make_migrated()
        os.unlink(str(self.jobs_dir / (JOB_ID + ".json")))

        result = self._run()

        self.assertTrue(result.blocked)
        self.assertEqual(result.error_code, ERROR_RECOVERY)
        self.assertTrue(self.ops.is_junction(str(self.source)))
        self.assertTrue(self.final.exists())

    # -- already rolled back / residual copy -----------------------------

    def test_already_rolled_back(self):
        result = self._run()

        self.assertTrue(result.ok)
        self.assertEqual(result.state, STATE_ALREADY_ROLLED_BACK)
        self.assertTrue(result.data_kept)
        self.assertFalse(result.link_removed)
        self.assertEqual(snapshot(self.source), expected_snapshot())

    def test_residual_copy_needs_explicit_confirmation(self):
        self.fixture.make_final(owned=True)

        result = self._run()

        self.assertEqual(result.state, STATE_RESIDUAL_COPY)
        self.assertTrue(result.residual_copy)
        self.assertTrue(result.blocked)
        self.assertTrue(self.final.exists())
        self.assertEqual(snapshot(self.source), expected_snapshot())

    def test_confirmed_residual_copy_is_removed(self):
        self.fixture.make_final(owned=True)

        result = self._run(self._request(confirm_residual_delete=True))

        self.assertEqual(result.state, STATE_RESIDUAL_COPY)
        self.assertTrue(result.target_removed)
        self.assertFalse(result.residual_copy)
        self.assertFalse(self.final.exists())
        self.assertEqual(snapshot(self.source), expected_snapshot())

    def test_residual_delete_is_refused_when_content_differs(self):
        """T26/R-02: same size, different bytes must not be deleted."""
        self.fixture.make_final(owned=True)
        (self.final / "a.bin").write_bytes(
            b"c" * len(FILE_PAYLOADS["a.bin"])
        )

        result = self._run(self._request(confirm_residual_delete=True))

        self.assertEqual(result.state, STATE_RESIDUAL_COPY)
        self.assertTrue(result.residual_copy)
        self.assertFalse(result.target_removed)
        self.assertEqual(result.error_code, ERROR_VERIFY)
        self.assertTrue((self.final / "a.bin").is_file())
        self.assertEqual(snapshot(self.source), expected_snapshot())

    # -- T26/R-02: content is re-checked before any target deletion ------

    def test_same_size_corruption_during_copy_fails_verification(self):
        self._make_migrated()
        payload = FILE_PAYLOADS["a.bin"]

        def transfer(source, staging, log_path, cancel_token=None):
            shutil.copytree(str(source), str(staging))
            # Another program rewrites the target while rollback copies it:
            # same byte count, different bytes.
            (Path(source) / "a.bin").write_bytes(b"c" * len(payload))
            return 0

        result = self._run(transfer=transfer)

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, ERROR_VERIFY)
        self.assertEqual(result.state, STATE_ROLLBACK_FAILED)
        self.assertTrue(result.data_kept)
        self.assertTrue(result.relinked)
        self.assertTrue(self.final.exists())
        self.assertTrue(self.ops.is_junction(str(self.source)))

    def test_same_size_corruption_before_cleanup_keeps_the_target(self):
        self._make_migrated()
        self._make_config_backup()
        payload = FILE_PAYLOADS["a.bin"]
        original = rollback_engine.restore_configs

        def corrupt_after_restore(*args, **kwargs):
            outcome = original(*args, **kwargs)
            # The restored source is already in place; the target changes
            # before the cleanup step is allowed to delete it.
            (self.final / "a.bin").write_bytes(b"z" * len(payload))
            return outcome

        with mock.patch.object(
            rollback_engine, "restore_configs", new=corrupt_after_restore
        ):
            result = self._run()

        self.assertTrue(result.source_restored)
        self.assertTrue(result.residual_copy)
        self.assertFalse(result.target_removed)
        self.assertEqual(result.error_code, ERROR_VERIFY)
        self.assertIn("refusing to delete", result.error_message)
        self.assertTrue((self.final / "a.bin").is_file())
        self.assertEqual(snapshot(self.source), expected_snapshot())

    # -- failure paths keep both copies ----------------------------------

    def test_copy_failure_keeps_both_copies_and_relinks(self):
        self._make_migrated()

        result = self._run(transfer=self._copier(exit_code=8, copy=False))

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, ERROR_COPY)
        self.assertEqual(result.state, STATE_ROLLBACK_FAILED)
        self.assertTrue(result.relinked)
        self.assertTrue(result.data_kept)
        self.assertTrue(self.final.exists())
        self.assertTrue(self.ops.is_junction(str(self.source)))
        self.assertFalse(os.path.lexists(self._restore_slot()))

    def test_verification_failure_keeps_both_copies_and_relinks(self):
        self._make_migrated()

        result = self._run(transfer=self._copier(exit_code=0, copy=False))

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, ERROR_VERIFY)
        self.assertEqual(result.state, STATE_ROLLBACK_FAILED)
        self.assertTrue(result.relinked)
        self.assertTrue(result.data_kept)
        self.assertTrue(self.final.exists())
        self.assertTrue(self.ops.is_junction(str(self.source)))
        self.assertFalse(os.path.lexists(self._restore_slot()))

    def test_relink_failure_requires_manual_recovery(self):
        self._make_migrated()
        self.ops.fail_create_link = "injected"

        result = self._run(transfer=self._copier(exit_code=8, copy=False))

        self.assertEqual(result.state, "ROLLBACK_RECOVERY_REQUIRED")
        self.assertEqual(result.error_code, ERROR_RECOVERY)
        self.assertTrue(result.data_kept)
        self.assertTrue(self.final.exists())
        self.assertFalse(result.relinked)

    def test_result_serialises_to_a_dict(self):
        self._make_migrated()

        result = self._run()
        data = result.to_dict()

        self.assertEqual(data["state"], STATE_ROLLED_BACK)
        self.assertTrue(data["ok"])
        self.assertTrue(data["target_removed"])
        self.assertEqual(data["job_id"], JOB_ID)


if __name__ == "__main__":
    unittest.main()
