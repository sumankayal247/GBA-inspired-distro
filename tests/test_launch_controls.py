#!/usr/bin/env python3
"""
Unit tests for GBA OS Launch Confirmation and InputMonitor.
"""

import unittest
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from gba_launcher import GBALauncher
from gba_adapter_mgba import ensure_mgba_config

class TestLaunchControls(unittest.TestCase):
    def test_ensure_mgba_config(self):
        ensure_mgba_config()
        cfg = os.path.expanduser("~/.config/mgba/config.ini")
        self.assertTrue(os.path.exists(cfg))
        with open(cfg, "r") as f:
            content = f.read()
        self.assertIn("fullscreen=1", content)
        self.assertIn("keyStart=13", content)
        self.assertIn("keyA=120", content)

    def test_launcher_screen_to_canvas(self):
        os.environ["SDL_VIDEODRIVER"] = "dummy"
        os.environ["SDL_AUDIODRIVER"] = "dummy"
        launcher = GBALauncher(screenshot_path="/tmp/test_sc.png")
        # In screenshot mode screen_w is CANVAS_WIDTH (480)
        cx, cy = launcher.screen_to_canvas(240, 160)
        self.assertEqual(cx, 240)
        self.assertEqual(cy, 160)

    def test_appliance_mode_flag(self):
        launcher = GBALauncher(appliance_mode=True, screenshot_path="/tmp/test_sc.png")
        self.assertTrue(launcher.appliance_mode)

if __name__ == "__main__":
    unittest.main()
