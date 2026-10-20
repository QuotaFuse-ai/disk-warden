# -*- coding: utf-8 -*-
"""S6 rename resilience: bounded retry and holder-aware error reporting.

Windows refuses to rename a directory while a file below it is open without
``FILE_SHARE_DELETE``.  These tests inject the denial, the retry clock and the
holder lookup so the behaviour is verified without a real lock.
"""

import errno
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.migrate.swap import SwapError, ensure_source_renamed  # noqa: E402
from tests.migrate_fakes import (  # noqa: E402
    JOB_ID,
    FakeOps,
    MigrationFixture,
    Process,
)


class _DenyingOps(FakeOps):
    """FakeOps whose rename is refused for the first ``failures`` attempts."""

    def __init__(self, failures):
        super(_DenyingOps, self).__init__()
        self.remaining = failures
        self.attempts = 0

    def rename(self, source, destination):
        self.attempts += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise OSError(errno.EACCES, "injected access denied")
        return super(_DenyingOps, self).rename(source, destination)


class _AccessDeniedWithoutErrno(FakeOps):
    """Mirrors Windows' ``PermissionError(winerror=5)`` shape."""

    def __init__(self):
        super(_AccessDeniedWithoutErrno, self).__init__()
        self.attempts = 0

    def rename(self, source, destination):
        self.attempts += 1
        exc = OSError("injected access denied")
        exc.winerror = 5
        raise exc


class SourceRenameTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory(
            prefix="dw-t10-swap-"
        )
        self.addCleanup(self._temporary.cleanup)
        self.fixture = MigrationFixture(Path(self._temporary.name))
        self.source = str(self.fixture.source)
        self.tombstone = str(self.fixture.tombstone)
        self.sleeps = []

    def rename(self, ops, holder_provider=None, delays=(0, 0, 0),
               integrity_provider=None):
        return ensure_source_renamed(
            ops,
            self.source,
            self.tombstone,
            JOB_ID,
            holder_provider=holder_provider or (lambda path: []),
            sleep=self.sleeps.append,
            retry_delays=delays,
            integrity_provider=integrity_provider or (lambda path: ""),
        )

    def test_transient_denial_is_retried_until_it_succeeds(self):
        ops = _DenyingOps(failures=2)

        renamed = self.rename(ops)

        self.assertTrue(renamed)
        self.assertEqual(ops.attempts, 3)
        self.assertEqual(len(self.sleeps), 2)
        self.assertFalse(self.fixture.source.exists())
        self.assertTrue(self.fixture.tombstone.is_dir())

    def test_persistent_denial_names_the_holding_processes(self):
        ops = _AccessDeniedWithoutErrno()

        with self.assertRaises(SwapError) as raised:
            self.rename(
                ops,
                holder_provider=lambda path: [
                    Process(4321, "Trae CN.exe"),
                    Process(99, ""),
                ],
            )

        message = str(raised.exception)
        self.assertIn("cannot rename source to tombstone", message)
        self.assertIn("Trae CN.exe (PID 4321)", message)
        self.assertIn("unknown (PID 99)", message)
        self.assertEqual(ops.attempts, 4)
        self.assertEqual(len(self.sleeps), 3)
        self.assertTrue(self.fixture.source.is_dir())
        self.assertFalse(self.fixture.tombstone.exists())

    def test_persistent_denial_without_holders_explains_why(self):
        ops = _DenyingOps(failures=99)

        with self.assertRaises(SwapError) as raised:
            self.rename(ops)

        message = str(raised.exception)
        self.assertIn("working directory", message)
        self.assertIn("indexer", message)
        self.assertTrue(self.fixture.source.is_dir())

    def test_unrelated_rename_error_is_not_retried(self):
        ops = FakeOps()
        ops.fail_rename = lambda source, destination: True

        with self.assertRaises(SwapError) as raised:
            self.rename(ops, holder_provider=lambda path: self.fail("called"))

        self.assertEqual(self.sleeps, [])
        self.assertIn("injected rename failure", str(raised.exception))

    def test_zero_retry_attempts_still_reports_holders(self):
        ops = _DenyingOps(failures=1)

        with self.assertRaises(SwapError) as raised:
            self.rename(
                ops,
                holder_provider=lambda path: [Process(7, "SearchIndexer.exe")],
                delays=(),
            )

        self.assertEqual(ops.attempts, 1)
        self.assertEqual(self.sleeps, [])
        self.assertIn("SearchIndexer.exe (PID 7)", str(raised.exception))

    def test_holder_lookup_failure_does_not_mask_the_rename_error(self):
        ops = _DenyingOps(failures=99)

        def explode(path):
            raise RuntimeError("restart manager unavailable")

        with self.assertRaises(SwapError) as raised:
            self.rename(ops, holder_provider=explode, delays=())

        self.assertIn("cannot rename source to tombstone", str(raised.exception))

    def test_integrity_denial_is_reported_without_retrying(self):
        suffix = (
            "; the app runs at integrity level Low but the folder requires"
            " Medium"
        )

        with self.assertRaises(SwapError) as raised:
            self.rename(
                FakeOps(),
                holder_provider=lambda path: self.fail("holders consulted"),
                integrity_provider=lambda path: suffix,
            )

        message = str(raised.exception)
        self.assertIn("cannot rename source to tombstone", message)
        self.assertIn("integrity level Low", message)
        self.assertEqual(self.sleeps, [])
        self.assertTrue(self.fixture.source.is_dir())
        self.assertFalse(self.fixture.tombstone.exists())

    def test_integrity_lookup_failure_still_allows_the_rename(self):
        def explode(path):
            raise RuntimeError("integrity query unavailable")

        renamed = self.rename(
            FakeOps(), integrity_provider=explode
        )

        self.assertTrue(renamed)
        self.assertTrue(self.fixture.tombstone.is_dir())


if __name__ == "__main__":
    unittest.main()
