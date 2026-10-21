# -*- coding: utf-8 -*-
"""System tray controller (T15): show / hide / open logs / quit."""

import unittest
from unittest import mock

from src.app.tray import TrayController, labels_for


class FakeWindow(object):
    def __init__(self):
        self.calls = []

    def show(self):
        self.calls.append("show")

    def restore(self):
        self.calls.append("restore")

    def hide(self):
        self.calls.append("hide")

    def destroy(self):
        self.calls.append("destroy")


class FakeIcon(object):
    def __init__(self):
        self.notifications = []
        self.stopped = False
        self.ran = False

    def run(self):
        self.ran = True

    def stop(self):
        self.stopped = True

    def notify(self, message, title):
        self.notifications.append((title, message))


def make_tray(window=None, logs_dir="", language="zh", on_quit=None):
    window = window or FakeWindow()
    icon = FakeIcon()
    seen = {}

    def factory(labels, item_cls):
        seen["labels"] = labels
        seen["item_cls"] = item_cls
        return icon

    tray = TrayController(
        window,
        logs_dir=logs_dir,
        language=language,
        on_quit=on_quit,
        icon_factory=factory,
    )
    return tray, window, icon, seen


class TrayLabelsTest(unittest.TestCase):
    def test_chinese_labels(self):
        labels = labels_for("zh-CN")
        self.assertEqual(labels["show"], "显示主窗口")
        self.assertEqual(labels["quit"], "退出 Disk Warden")

    def test_english_labels_for_other_languages(self):
        labels = labels_for("en")
        self.assertEqual(labels["show"], "Show Disk Warden")
        self.assertEqual(labels_for("")["quit"], "Quit Disk Warden")


class TrayControllerTest(unittest.TestCase):
    def test_start_builds_icon_and_runs_it_on_a_daemon_thread(self):
        tray, _window, icon, seen = make_tray()
        self.assertFalse(tray.running)
        self.assertTrue(tray.start())
        self.assertTrue(tray.running)
        self.assertEqual(seen["labels"]["title"], "Disk Warden")
        self.assertTrue(icon.ran)
        self.assertTrue(tray._thread.daemon)

    def test_start_is_idempotent(self):
        tray, _window, _icon, _seen = make_tray()
        self.assertTrue(tray.start())
        first = tray._icon
        self.assertTrue(tray.start())
        self.assertIs(tray._icon, first)

    def test_start_returns_false_when_the_icon_cannot_be_built(self):
        def boom(labels, item_cls):
            raise RuntimeError("no tray")

        tray = TrayController(FakeWindow(), icon_factory=boom)
        self.assertFalse(tray.start())
        self.assertFalse(tray.running)

    def test_show_window_shows_and_restores(self):
        tray, window, _icon, _seen = make_tray()
        tray.start()
        tray.show_window()
        self.assertEqual(window.calls, ["show", "restore"])

    def test_hide_window_notifies_only_once(self):
        tray, window, icon, _seen = make_tray()
        tray.start()
        tray.hide_window()
        tray.hide_window()
        self.assertEqual(window.calls, ["hide", "hide"])
        self.assertEqual(len(icon.notifications), 1)
        self.assertIn("托盘", icon.notifications[0][1])

    def test_hide_window_can_skip_the_notification(self):
        tray, window, icon, _seen = make_tray()
        tray.start()
        tray.hide_window(notify=False)
        self.assertEqual(window.calls, ["hide"])
        self.assertEqual(icon.notifications, [])

    def test_open_logs_reveals_the_log_directory(self):
        tray, _window, _icon, _seen = make_tray(logs_dir=r"D:\data\logs")
        tray.start()
        with mock.patch("src.platform.reveal_directory") as reveal:
            tray.open_logs()
        reveal.assert_called_once_with(r"D:\data\logs")

    def test_open_logs_without_a_directory_does_nothing(self):
        tray, _window, _icon, _seen = make_tray()
        tray.start()
        with mock.patch("src.platform.reveal_directory") as reveal:
            tray.open_logs()
        reveal.assert_not_called()

    def test_quit_runs_the_callback_once_and_stops_the_icon(self):
        calls = []
        tray, _window, icon, _seen = make_tray(on_quit=lambda: calls.append(True))
        tray.start()
        tray.quit()
        tray.quit()
        self.assertEqual(calls, [True])
        self.assertTrue(icon.stopped)
        self.assertFalse(tray.running)

    def test_quit_without_callback_destroys_the_window(self):
        tray, window, _icon, _seen = make_tray()
        tray.start()
        tray.quit()
        self.assertEqual(window.calls, ["destroy"])

    def test_stop_is_idempotent(self):
        tray, _window, icon, _seen = make_tray()
        tray.start()
        tray.stop()
        tray.stop()
        self.assertTrue(icon.stopped)
        self.assertFalse(tray.running)

    def test_notify_is_safe_when_the_icon_is_gone(self):
        tray, _window, _icon, _seen = make_tray()
        tray.notify("title", "message")  # must not raise


if __name__ == "__main__":
    unittest.main()
