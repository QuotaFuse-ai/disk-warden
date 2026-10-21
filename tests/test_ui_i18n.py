# -*- coding: utf-8 -*-
"""The UI ships both dictionaries; English is the default.

These tests pin the language contract requested for the beta: the app opens in
English, no Chinese leaks into an English session, and every literal
translation key used by the page resolves.
"""

import io
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "ui", "index.html")


def _page():
    with io.open(PAGE, encoding="utf-8") as handle:
        return handle.read()


def _dict_block(page, lang):
    match = re.search(r"I18N\['%s'\] = \{(.*?)\n\};" % lang, page, re.S)
    return match.group(1) if match else ""


def _keys(block):
    return set(re.findall(r"^\s*'([^']+)':", block, re.M))


class UiLanguageTests(unittest.TestCase):
    def setUp(self):
        self.page = _page()
        self.zh = _dict_block(self.page, "zh-CN")
        self.en = _dict_block(self.page, "en")

    def test_both_dictionaries_are_present(self):
        self.assertTrue(self.zh)
        self.assertTrue(self.en)

    def test_dictionaries_have_the_same_keys(self):
        zh, en = _keys(self.zh), _keys(self.en)
        self.assertEqual(sorted(zh - en), [], "keys missing from the English dictionary")
        self.assertEqual(sorted(en - zh), [], "keys missing from the Chinese dictionary")

    def test_english_is_the_default_language(self):
        self.assertIn("let LANG = 'en';", self.page)
        self.assertIn('<html lang="en">', self.page)
        self.assertNotIn("let LANG = 'zh-CN';", self.page)

    def test_english_sessions_never_fall_back_to_chinese(self):
        self.assertNotIn("s = I18N['zh-CN'][key]", self.page)
        self.assertNotIn("I18N[LANG] || I18N['zh-CN']", self.page)

    def test_every_literal_key_used_by_the_page_exists(self):
        used = set(re.findall(r"\bt\('([A-Za-z][\w.]*)'", self.page))
        used |= set(re.findall(r"t\('([A-Za-z][\w.]*)',", self.page))
        # Keys built at runtime ("reason." + key, "state." + code, ...) are
        # covered by their prefix families.
        prefixes = ("reason.", "state.", "blockedCode.", "ide.kind.", "ai.", "welcome.point")
        missing = sorted(
            key for key in used
            if key not in _keys(self.en)
            and not key.startswith(prefixes)
            and not key.endswith(".")  # fragment of a "prefix + value" lookup
        )
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
