"""Startup animation and responsive, connection-only status-bar behaviour."""

import os
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame  # noqa: E402

import gba_launcher  # noqa: E402
from gba_boot import BootAnimation  # noqa: E402
from gba_status import SystemStatusMonitor  # noqa: E402
from tests.test_shell import ShellTestCase  # noqa: E402
from theme import Theme  # noqa: E402


class TestSystemStatusMonitor(unittest.TestCase):
    def test_refresh_returns_disconnected_snapshots_and_tolerates_failures(self):
        wifi = {"connected": True, "ssid": "Pocket", "signal": 80}
        monitor = SystemStatusMonitor(
            wifi_provider=lambda: wifi,
            bluetooth_provider=lambda: {"powered": True, "connected": False})
        monitor.refresh()
        snapshot = monitor.snapshot()
        self.assertEqual(snapshot["wifi"]["ssid"], "Pocket")
        self.assertFalse(snapshot["bluetooth"]["connected"])
        snapshot["wifi"]["ssid"] = "changed outside"
        self.assertEqual(monitor.snapshot()["wifi"]["ssid"], "Pocket")

        monitor.wifi_provider = lambda: (_ for _ in ()).throw(OSError("slow bus"))
        monitor.bluetooth_provider = lambda: None
        monitor.refresh()                    # no provider exception reaches UI
        self.assertIsNone(monitor.snapshot()["wifi"])
        self.assertIsNone(monitor.snapshot()["bluetooth"])

    def test_start_does_not_block_on_a_slow_hardware_probe(self):
        entered = threading.Event()
        release = threading.Event()

        def slow_wifi():
            entered.set()
            release.wait(1.0)
            return {"connected": True}

        monitor = SystemStatusMonitor(slow_wifi, lambda: None, interval=1.0)
        started = time.monotonic()
        monitor.start()
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertTrue(entered.wait(0.5))
        self.assertIsNone(monitor.snapshot()["wifi"])
        release.set()
        deadline = time.monotonic() + 1.0
        while monitor.snapshot()["wifi"] is None and time.monotonic() < deadline:
            time.sleep(0.005)
        monitor.stop()
        self.assertTrue(monitor.snapshot()["wifi"]["connected"])


class TestBootAnimation(unittest.TestCase):
    def setUp(self):
        pygame.init()
        pygame.font.init()
        self.surface = pygame.display.set_mode((960, 640))

    def test_timing_is_skippable_complete_and_bounded(self):
        animation = BootAnimation(min_duration=2.0, max_duration=4.0)
        self.assertFalse(animation.can_skip(0.2))
        self.assertTrue(animation.can_skip(0.5))
        self.assertFalse(animation.should_finish(1.9, ready=True))
        self.assertTrue(animation.should_finish(2.0, ready=True))
        self.assertFalse(animation.should_finish(2.5, ready=False))
        self.assertTrue(animation.should_finish(4.0, ready=False))

    def test_animation_draws_at_early_logo_and_extended_scan_frames(self):
        animation = BootAnimation()
        theme = Theme("indigo", 2.0)
        for elapsed in (0.0, 0.4, 1.8, 4.5):
            animation.draw(self.surface, theme, elapsed, loading=True)
        animation.draw(self.surface, theme, 2.5, loading=False)
        self.assertEqual(self.surface.get_size(), (960, 640))


class TestStatusBar(ShellTestCase):
    def test_draw_never_calls_hardware_probes_or_usb_label(self):
        launcher = self.make_launcher()
        launcher.status_monitor = SimpleNamespace(snapshot=lambda: {
            "wifi": {"connected": False},
            "bluetooth": {"powered": True, "connected": False},
        })
        with patch.object(gba_launcher.settings_store, "get_wifi_status",
                          side_effect=AssertionError("synchronous WiFi probe")), \
                patch.object(gba_launcher.settings_store, "get_bluetooth_status",
                             side_effect=AssertionError("synchronous Bluetooth probe")), \
                patch.object(launcher, "usb_status_text",
                             side_effect=AssertionError("USB chip is back")), \
                patch.object(pygame.draw, "arc", wraps=pygame.draw.arc) as arcs, \
                patch.object(pygame.draw, "lines", wraps=pygame.draw.lines) as lines:
            launcher.draw_top_bar()
        arcs.assert_not_called()
        lines.assert_not_called()

    def test_wifi_and_bluetooth_icons_draw_only_when_connected(self):
        launcher = self.make_launcher()
        launcher.status_monitor = SimpleNamespace(snapshot=lambda: {
            "wifi": {"connected": True, "signal": 75},
            "bluetooth": {"powered": True, "connected": True},
        })
        with patch.object(pygame.draw, "arc", wraps=pygame.draw.arc) as arcs, \
                patch.object(pygame.draw, "lines", wraps=pygame.draw.lines) as lines:
            launcher.draw_top_bar()
        self.assertEqual(arcs.call_count, 3)
        lines.assert_called_once()
        for call in arcs.call_args_list:
            self.assertLess(call.args[3], call.args[4])  # valid start/end radians

    def test_selected_shelf_rows_have_no_right_chevron(self):
        launcher = self.make_launcher()
        launcher.goto_tab(gba_launcher.TAB_SETTINGS)
        with patch.object(launcher.theme, "label", wraps=launcher.theme.label) as label:
            launcher.draw_background()
        self.assertFalse(any(">" in call.args for call in label.call_args_list))


class TestRuntimePackaging(unittest.TestCase):
    def test_installed_shell_contains_boot_and_status_dependencies(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "scripts", "install.sh"), encoding="utf-8") as script:
            module_loop = script.read().split("for mod in", 1)[1].split("; do", 1)[0]
        for module in ("gba_boot.py", "gba_status.py", "gba_ingame_ui.py"):
            self.assertIn(module, module_loop)


if __name__ == "__main__":
    unittest.main()
