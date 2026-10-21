# -*- coding: utf-8 -*-
"""macOS packaging pieces that can be checked off a Mac (T21).

Covers the icon conversion, the launcher's platform branch and the ``--smoke``
report.  The PyInstaller build itself only runs on the CI macOS runner.
"""

import importlib.util
import json
import os
import struct
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, relative):
    path = os.path.join(ROOT, *relative.split("/"))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


make_icns = _load("dw_make_icns", "packaging/make_icns.py")
launcher = _load("dw_launcher_macos", "packaging/launcher.py")


def _synthetic_ico(*sizes):
    # type: (object) -> tuple
    """Build an ICO whose entries are raw "PNG" blobs and return both sides."""
    blobs = {}
    for size in sizes:
        blobs[size] = make_icns.PNG_MAGIC + bytes([size % 251]) * 8
    header = struct.pack("<HHH", 0, 1, len(blobs))
    directory = b""
    body = b""
    offset = 6 + 16 * len(blobs)
    for size, blob in blobs.items():
        field = 0 if size >= 256 else size
        directory += struct.pack(
            "<BBBBHHII", field, field, 0, 0, 1, 32, len(blob), offset + len(body)
        )
        body += blob
    return header + directory + body, blobs


class IcoConversionTest(unittest.TestCase):
    def test_png_entries_are_read_by_size(self):
        data, blobs = _synthetic_ico(16, 32, 128, 256)
        self.assertEqual(make_icns.read_ico_entries(data), blobs)

    def test_non_png_entries_are_ignored(self):
        data, blobs = _synthetic_ico(32)
        broken = data[:6] + data[6:22] + b"BITMAP-DATA"
        self.assertEqual(make_icns.read_ico_entries(broken), {})

    def test_not_an_ico_is_rejected(self):
        with self.assertRaises(ValueError):
            make_icns.read_ico_entries(b"nope")

    def test_icns_wraps_each_known_size(self):
        _, blobs = _synthetic_ico(16, 32, 128, 256)
        data = make_icns.build_icns(blobs)
        self.assertEqual(data[:4], b"icns")
        self.assertEqual(struct.unpack(">I", data[4:8])[0], len(data))
        offset = 8
        found = []
        while offset < len(data):
            chunk_type = data[offset:offset + 4]
            length = struct.unpack(">I", data[offset + 4:offset + 8])[0]
            found.append((chunk_type, length))
            self.assertEqual(data[offset + 8:offset + 16], make_icns.PNG_MAGIC)
            offset += length
        self.assertEqual(
            found,
            [
                (b"icp4", len(blobs[16]) + 8),
                (b"icp5", len(blobs[32]) + 8),
                (b"ic07", len(blobs[128]) + 8),
                (b"ic08", len(blobs[256]) + 8),
            ],
        )
        self.assertEqual(offset, len(data))

    def test_missing_sizes_are_skipped_not_upscaled(self):
        _, blobs = _synthetic_ico(16, 256)
        data = make_icns.build_icns(blobs)
        self.assertIn(b"icp4", data)
        self.assertIn(b"ic08", data)
        self.assertNotIn(b"icp5", data)
        self.assertNotIn(b"ic07", data)

    def test_no_usable_entries_is_an_error(self):
        with self.assertRaises(ValueError):
            make_icns.build_icns({})

    def test_convert_writes_a_file(self):
        data, blobs = _synthetic_ico(16, 32)
        with tempfile.TemporaryDirectory() as tmp:
            ico = os.path.join(tmp, "icon.ico")
            icns = os.path.join(tmp, "icon.icns")
            with open(ico, "wb") as stream:
                stream.write(data)
            used = make_icns.convert(ico, icns)
            self.assertEqual(used, [b"icp4", b"icp5"])
            with open(icns, "rb") as stream:
                self.assertEqual(stream.read()[:4], b"icns")

    def test_shipped_icon_converts(self):
        """The committed Windows icon must keep producing a usable .icns."""
        ico = os.path.join(ROOT, "packaging", "DiskWarden.ico")
        self.assertTrue(os.path.isfile(ico), ico)
        with open(ico, "rb") as stream:
            entries = make_icns.read_ico_entries(stream.read())
        self.assertIn(256, entries)
        self.assertIn(128, entries)


class LauncherPlatformBranchTest(unittest.TestCase):
    def test_runtime_layout_check_is_windows_only(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            launcher.sys, "frozen", True, create=True
        ), mock.patch.object(
            launcher.sys, "platform", "darwin"
        ), mock.patch.object(launcher, "_bundled_python_dll", return_value=""), mock.patch.object(
            launcher, "_stale_python_dlls", return_value=["python38.dll"]
        ):
            self.assertTrue(launcher._check_runtime_layout())

    def test_mac_alert_passes_the_text_as_an_argument(self):
        text = 'quote " and newline\nand backslash \\ stay literal'
        with mock.patch.object(launcher.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0)
            self.assertTrue(launcher._mac_message(text))
        arguments = run.call_args[0][0]
        self.assertEqual(arguments[0], "/usr/bin/osascript")
        self.assertIn(launcher._MAC_ALERT_SCRIPT, arguments)
        self.assertEqual(arguments[-1], text)
        self.assertNotIn(text, " ".join(arguments[:-1]))

    def test_mac_alert_failure_falls_back_to_stderr(self):
        with mock.patch.object(launcher.sys, "platform", "darwin"), mock.patch.object(
            launcher, "_mac_message", return_value=False
        ), mock.patch.object(launcher.sys, "stderr") as stderr:
            launcher._message("boom")
        stderr.write.assert_called_once_with("boom\n")

    def test_a_missing_stderr_does_not_raise(self):
        with mock.patch.object(launcher.sys, "platform", "darwin"), mock.patch.object(
            launcher, "_mac_message", return_value=False
        ), mock.patch.object(launcher.sys, "stderr", None):
            launcher._message("boom")


class SmokeReportTest(unittest.TestCase):
    def test_destination_parsing(self):
        self.assertEqual(launcher._smoke_destination(["--smoke"]), "")
        self.assertEqual(
            launcher._smoke_destination(["--smoke-out", "out.json"]), "out.json"
        )
        self.assertEqual(
            launcher._smoke_destination(["--smoke-out=out.json"]), "out.json"
        )
        self.assertEqual(launcher._smoke_destination(["--smoke-out"]), "")

    def test_report_describes_the_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = launcher._smoke_report(tmp)
        self.assertFalse(report["ok"])  # not frozen under the test runner
        self.assertFalse(report["frozen"])
        self.assertTrue(report["ui_present"], report)
        self.assertTrue(report["data_root_writable"], report)
        self.assertIn("link_support", report)
        self.assertIsInstance(report["errors"], list)
        self.assertTrue(report["ui_path"].endswith(os.path.join("ui", "index.html")))

    def test_smoke_writes_json_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            launcher, "_prepare_data_root", return_value=True
        ), mock.patch.dict(
            os.environ, {"DISKWARDEN_DATA_ROOT": tmp}
        ), mock.patch.object(
            launcher, "_smoke_report", return_value={"ok": False, "errors": ["x"]}
        ):
            target = os.path.join(tmp, "logs", "smoke.json")
            self.assertEqual(launcher._run_smoke(["--smoke-out", target]), 1)
            with open(target, "r", encoding="utf-8") as stream:
                self.assertEqual(json.load(stream), {"ok": False, "errors": ["x"]})

    def test_smoke_returns_zero_only_when_the_report_passes(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            launcher, "_prepare_data_root", return_value=True
        ), mock.patch.dict(
            os.environ, {"DISKWARDEN_DATA_ROOT": tmp}
        ), mock.patch.object(
            launcher, "_smoke_report", return_value={"ok": True}
        ):
            self.assertEqual(launcher._run_smoke(["--smoke"]), 0)
            self.assertTrue(os.path.isfile(os.path.join(tmp, "logs", "smoke.json")))

    def test_smoke_stops_when_no_data_root_is_writable(self):
        with mock.patch.object(launcher, "_prepare_data_root", return_value=False):
            self.assertEqual(launcher._run_smoke(["--smoke"]), 1)


class DefaultDataRootTest(unittest.TestCase):
    def test_macos_uses_application_support(self):
        from src.core.store import layout

        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            layout.sys, "platform", "darwin"
        ), mock.patch.object(
            layout.os.path, "expanduser", return_value="/Users/alice"
        ):
            self.assertEqual(
                layout.default_data_root(),
                os.path.join(
                    "/Users/alice", "Library", "Application Support", "DiskWarden"
                ),
            )

    def test_environment_override_still_wins_on_macos(self):
        from src.core.store import layout

        with mock.patch.dict(
            os.environ, {layout.ENV_DATA_ROOT: "/Volumes/Data/DiskWarden"}
        ), mock.patch.object(layout.sys, "platform", "darwin"):
            self.assertEqual(
                layout.default_data_root(), "/Volumes/Data/DiskWarden"
            )


if __name__ == "__main__":
    unittest.main()
