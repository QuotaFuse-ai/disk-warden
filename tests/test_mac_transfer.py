# -*- coding: utf-8 -*-
"""macOS rsync adapter: argument contract, exit-code mapping, cancel (T21)."""

import os
import subprocess
import threading
import unittest
from unittest import mock

from src.platform.mac import transfer as mac_transfer
from src.platform.transfer import TransferCancelled, TransferError


class NormaliseExitCodeTest(unittest.TestCase):
    def test_success_codes(self):
        self.assertEqual(mac_transfer.normalise_exit_code(0), 0)
        # 24 = some files vanished; verification stays authoritative.
        self.assertEqual(mac_transfer.normalise_exit_code(24), 0)

    def test_low_codes_fail_instead_of_looking_like_robocopy_success(self):
        for code in (1, 2, 3, 4, 5, 6, 7):
            self.assertEqual(mac_transfer.normalise_exit_code(code), 8)

    def test_high_codes_keep_their_detail(self):
        for code in (8, 11, 12, 23):
            self.assertEqual(mac_transfer.normalise_exit_code(code), code)


class SourceArgumentTest(unittest.TestCase):
    def test_directory_contents_are_copied(self):
        # POSIX separators regardless of the host running the tests.
        self.assertEqual(
            mac_transfer._source_argument("/Users/alice/Cache"),
            "/Users/alice/Cache/",
        )
        # An existing trailing slash is not doubled.
        self.assertEqual(
            mac_transfer._source_argument("/Users/alice/Cache/"),
            "/Users/alice/Cache/",
        )
        # A source that *is* the volume root stays valid.
        self.assertEqual(mac_transfer._source_argument("/"), "/")


class RunRsyncTest(unittest.TestCase):
    def _run(self, wait_result=None, wait_side_effect=None, token=None):
        process = mock.Mock()
        if wait_side_effect is not None:
            process.wait.side_effect = wait_side_effect
        else:
            process.wait.return_value = wait_result
        with mock.patch.object(
            mac_transfer.subprocess, "Popen", return_value=process
        ) as popen:
            code = mac_transfer.run_robocopy(
                "/Users/alice/Cache",
                "/Users/alice/DiskWarden/staging",
                "/Users/alice/DiskWarden/staging/rsync.log",
                token,
            )
        return code, popen, process

    def test_success_returns_zero(self):
        code, popen, _ = self._run(wait_result=0)
        self.assertEqual(code, 0)
        argv = popen.call_args[0][0]
        self.assertEqual(argv[0], "rsync")
        self.assertIn("--log-file=/Users/alice/DiskWarden/staging/rsync.log", argv)
        self.assertIn("--", argv)
        self.assertEqual(argv[-2], "/Users/alice/Cache/")
        self.assertEqual(argv[-1], "/Users/alice/DiskWarden/staging")
        self.assertNotIn("--delete", argv)

    def test_failure_is_reported_above_the_success_band(self):
        code, _, _ = self._run(wait_result=23)
        self.assertEqual(code, 23)
        self.assertGreaterEqual(code, 8)

    def test_low_error_code_is_normalised(self):
        code, _, _ = self._run(wait_result=3)
        self.assertEqual(code, 8)

    def test_cancel_kills_the_process(self):
        token = threading.Event()
        token.set()
        with self.assertRaises(TransferCancelled):
            self._run(
                wait_side_effect=subprocess.TimeoutExpired("rsync", 0.2),
                token=token,
            )

    def test_missing_binary_is_a_transfer_error(self):
        with mock.patch.object(
            mac_transfer.subprocess, "Popen", side_effect=OSError("no rsync")
        ):
            with self.assertRaises(TransferError):
                mac_transfer.run_robocopy("src", "dst", "log")

    def test_empty_arguments_are_rejected(self):
        with self.assertRaises(ValueError):
            mac_transfer.run_robocopy("", "dst", "log")


class CancelKillTest(unittest.TestCase):
    def test_kill_failure_is_swallowed(self):
        process = mock.Mock()
        process.kill.side_effect = OSError("gone")
        process.wait.side_effect = subprocess.TimeoutExpired("rsync", 5)
        mac_transfer._stop(process)  # must not raise


if __name__ == "__main__":
    unittest.main()
