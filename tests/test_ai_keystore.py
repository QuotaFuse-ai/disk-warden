# -*- coding: utf-8 -*-
"""DPAPI-backed local storage for the AI API key (T25).

The round-trip tests need Windows DPAPI; the file/format tests do not.
"""

import base64
import os
import tempfile
import unittest

from src.core.ai import keystore

KEY = "sk-test-do-not-leak-123456"


class KeystoreFileTestBase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = os.path.join(temp.name, keystore.KEY_FILE_NAME)

    def read_bytes(self):
        with open(self.path, "rb") as handle:
            return handle.read()

    def write_bytes(self, payload):
        with open(self.path, "wb") as handle:
            handle.write(payload)


class AbsentAndClearTest(KeystoreFileTestBase):
    def test_absent_file_loads_as_empty_string(self):
        self.assertFalse(keystore.stored(self.path))
        self.assertEqual(keystore.load(self.path), "")

    def test_clear_reports_whether_a_file_was_removed(self):
        self.assertFalse(keystore.clear(self.path))
        self.write_bytes(b"anything")
        self.assertTrue(keystore.clear(self.path))
        self.assertFalse(os.path.exists(self.path))

    def test_empty_path_is_harmless(self):
        self.assertEqual(keystore.load(""), "")
        self.assertFalse(keystore.stored(""))
        self.assertFalse(keystore.clear(""))


class CorruptFileTest(KeystoreFileTestBase):
    def assert_corrupt(self, payload, code=keystore.CODE_CORRUPT):
        self.write_bytes(payload)
        with self.assertRaises(keystore.KeystoreError) as ctx:
            keystore.load(self.path)
        self.assertEqual(ctx.exception.code, code)
        self.assertNotIn(KEY, str(ctx.exception))

    def test_non_base64_file_is_rejected(self):
        self.assert_corrupt(b"this is not base64 !!!")

    def test_empty_file_is_rejected(self):
        self.assert_corrupt(b"")

    def test_oversized_file_is_rejected(self):
        self.assert_corrupt(base64.b64encode(b"x" * (keystore.MAX_FILE_BYTES + 8)))


@unittest.skipUnless(keystore.is_supported(), "DPAPI is only available on Windows")
class DpapiRoundTripTest(KeystoreFileTestBase):
    def test_save_load_roundtrip(self):
        keystore.save(self.path, KEY)
        self.assertTrue(keystore.stored(self.path))
        self.assertEqual(keystore.load(self.path), KEY)

    def test_file_holds_ciphertext_not_the_plaintext(self):
        keystore.save(self.path, KEY)
        raw = self.read_bytes()
        self.assertNotIn(KEY.encode("utf-8"), raw)
        protected = base64.b64decode(raw, validate=True)
        self.assertNotIn(KEY.encode("utf-8"), protected)

    def test_clear_removes_the_ciphertext(self):
        keystore.save(self.path, KEY)
        self.assertTrue(keystore.clear(self.path))
        self.assertFalse(keystore.stored(self.path))
        self.assertEqual(keystore.load(self.path), "")

    def test_tampered_blob_fails_to_decrypt(self):
        keystore.save(self.path, KEY)
        protected = bytearray(base64.b64decode(self.read_bytes(), validate=True))
        protected[len(protected) // 2] ^= 0xFF
        self.write_bytes(base64.b64encode(bytes(protected)))
        with self.assertRaises(keystore.KeystoreError) as ctx:
            keystore.load(self.path)
        self.assertEqual(ctx.exception.code, keystore.CODE_DECRYPT_FAILED)
        self.assertNotIn(KEY, str(ctx.exception))

    def test_empty_secret_is_rejected(self):
        with self.assertRaises(keystore.KeystoreError) as ctx:
            keystore.save(self.path, "")
        self.assertEqual(ctx.exception.code, keystore.CODE_ENCRYPT_FAILED)
        self.assertFalse(keystore.stored(self.path))

    def test_overlong_secret_is_rejected(self):
        with self.assertRaises(keystore.KeystoreError):
            keystore.save(self.path, "x" * (keystore.MAX_SECRET_BYTES + 1))
        self.assertFalse(keystore.stored(self.path))

    def test_write_failure_is_reported_with_a_stable_code(self):
        blocker = self.path + ".blocker"
        with open(blocker, "wb") as handle:
            handle.write(b"not a directory")
        missing = os.path.join(blocker, keystore.KEY_FILE_NAME)
        with self.assertRaises(keystore.KeystoreError) as ctx:
            keystore.save(missing, KEY)
        self.assertEqual(ctx.exception.code, keystore.CODE_SAVE_FAILED)
        self.assertNotIn(KEY, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
