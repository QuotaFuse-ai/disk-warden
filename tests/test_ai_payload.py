# -*- coding: utf-8 -*-
"""Privacy boundary tests for the AI payload (T22).

The payload must never carry a drive letter, the home directory, the user
name or any environment-variable value.  These tests exercise the sanitizers
directly and assert that the serialized payload is leak-free.
"""

import unittest

from src.core.ai.payload import (
    PAYLOAD_SCHEMA_VERSION,
    build_payload,
    detect_identity,
    extension_counts,
    has_leak,
    leak_categories,
    path_depth,
    sanitize_path,
    sanitize_segments,
    serialize_payload,
)

ENV = {
    "USERNAME": "alice",
    "USERPROFILE": r"C:\Users\alice",
    "APPDATA": r"C:\Users\alice\AppData\Roaming",
    "LOCALAPPDATA": r"C:\Users\alice\AppData\Local",
    "SECRET_TOKEN": "project-omega-42",
}


def item(**overrides):
    data = {
        "tool_id": "cursor",
        "source_path": r"C:\Users\alice\AppData\Roaming\Cursor\User\workspaceStorage",
        "rel_path": "User/workspaceStorage",
        "kind": "dir",
        "file_count": 120,
        "bytes": 4096,
    }
    data.update(overrides)
    return data


class IdentityTests(unittest.TestCase):
    def test_detect_identity_reads_user_and_home(self):
        self.assertEqual(
            detect_identity(ENV), ("alice", r"C:\Users\alice")
        )

    def test_detect_identity_is_empty_without_env(self):
        self.assertEqual(detect_identity({}), ("", ""))


class SegmentSanitizerTests(unittest.TestCase):
    def test_relative_path_is_preferred(self):
        self.assertEqual(
            sanitize_segments(item()["source_path"], rel_path="User/workspaceStorage", environ=ENV),
            ["User", "workspaceStorage"],
        )

    def test_absolute_path_drops_drive_home_and_username(self):
        segments = sanitize_segments(
            r"C:\Users\alice\AppData\Roaming\Cursor\User\workspaceStorage\blob",
            environ=ENV,
        )
        self.assertEqual(
            segments, ["User", "workspaceStorage", "blob"]
        )

    def test_username_is_replaced_when_home_prefix_does_not_apply(self):
        segments = sanitize_segments(r"D:\shared\alice\notes", environ=ENV)
        self.assertEqual(segments, ["shared", "<user>", "notes"])

    def test_environment_values_are_replaced(self):
        segments = sanitize_segments(
            r"D:\builds\project-omega-42\bin", environ=ENV
        )
        self.assertEqual(segments, ["builds", "<env>", "bin"])

    def test_unc_share_root_is_dropped(self):
        segments = sanitize_segments(
            r"\\fileserver\share\Projects\Cache", environ=ENV
        )
        self.assertEqual(segments, ["Projects", "Cache"])

    def test_segment_count_is_capped(self):
        segments = sanitize_segments(
            r"D:\a\b\c\d\e\f", environ=ENV, max_segments=2
        )
        self.assertEqual(segments, ["e", "f"])

    def test_segment_length_is_capped(self):
        segments = sanitize_segments(
            "D:\\" + "x" * 200, environ=ENV
        )
        self.assertEqual(len(segments[0]), 48)

    def test_sanitize_path_joins_fragments(self):
        self.assertEqual(
            sanitize_path(
                r"C:\Users\alice\AppData\Roaming\Cursor\Cache",
                environ=ENV,
            ),
            "Cursor/Cache",
        )


class DepthTests(unittest.TestCase):
    def test_depth_counts_relative_segments(self):
        self.assertEqual(
            path_depth(item()["source_path"], rel_path="User/workspaceStorage"),
            2,
        )

    def test_depth_is_capped(self):
        deep = "/".join("d%d" % index for index in range(64))
        self.assertEqual(path_depth(None, rel_path=deep), 32)


class ExtensionTests(unittest.TestCase):
    def test_missing_extensions_is_empty(self):
        self.assertEqual(extension_counts(item()), {})

    def test_extensions_are_normalised_and_merged(self):
        counts = extension_counts(
            item(extensions={".JS": 5, "log": 3, "weird/name": 1, "": 2, "x": 0})
        )
        self.assertEqual(counts[".js"], 5)
        self.assertEqual(counts[".log"], 3)
        self.assertEqual(counts["__other__"], 3)

    def test_only_top_twelve_are_kept(self):
        raw = {"e%02d" % index: 100 - index for index in range(15)}
        counts = extension_counts(item(extensions=raw))
        self.assertEqual(len(counts), 13)  # 12 kept + __other__
        self.assertIn("__other__", counts)
        self.assertEqual(counts["__other__"], sum(range(88, 85, -1)))


class PayloadTests(unittest.TestCase):
    def test_payload_contains_metadata_only(self):
        payload = build_payload(item(extensions={"log": 4}), environ=ENV)
        self.assertEqual(
            sorted(payload),
            [
                "depth",
                "extensions",
                "file_count",
                "path",
                "path_segments",
                "schema_version",
                "total_bytes",
            ],
        )
        self.assertEqual(payload["schema_version"], PAYLOAD_SCHEMA_VERSION)
        self.assertEqual(payload["path"], "User/workspaceStorage")
        self.assertEqual(payload["path_segments"], ["User", "workspaceStorage"])
        self.assertEqual(payload["file_count"], 120)
        self.assertEqual(payload["total_bytes"], 4096)
        self.assertEqual(payload["extensions"], {".log": 4})

    def test_absolute_source_path_is_sanitized(self):
        payload = build_payload(
            item(source_path=r"C:\Users\alice\AppData\Local\Cursor\Cache", rel_path=None),
            environ=ENV,
        )
        self.assertEqual(payload["path"], "Cursor/Cache")

    def test_serialized_payload_has_no_leak(self):
        payload = build_payload(
            item(
                source_path=r"C:\Users\alice\AppData\Local\project-omega-42\cache",
                rel_path=None,
            ),
            environ=ENV,
        )
        text = serialize_payload(payload)
        self.assertEqual(leak_categories(text, environ=ENV), [])
        self.assertNotIn("alice", text.casefold())
        self.assertNotIn("c:", text.casefold())
        self.assertNotIn("project-omega-42", text)

    def test_serialization_is_deterministic(self):
        payload = build_payload(item(), environ=ENV)
        self.assertEqual(serialize_payload(payload), serialize_payload(payload))


class LeakDetectionTests(unittest.TestCase):
    def test_username_and_home_are_detected(self):
        found = leak_categories(r"x alice C:\Users\alice\x", environ=ENV)
        self.assertIn("username", found)
        self.assertIn("home_directory", found)

    def test_drive_letter_is_detected(self):
        self.assertIn(
            "drive_letter",
            leak_categories(r"C:\Users\alice", environ=ENV),
        )
        self.assertIn(
            "drive_letter",
            leak_categories("D:/data", environ=ENV),
        )

    def test_environment_value_is_detected(self):
        self.assertIn(
            "environment_value",
            leak_categories("build project-omega-42", environ=ENV),
        )

    def test_clean_text_has_no_leak(self):
        self.assertFalse(has_leak('{"path":"Cursor/Cache"}', environ=ENV))


if __name__ == "__main__":
    unittest.main()
