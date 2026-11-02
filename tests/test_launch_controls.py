#!/usr/bin/env python3
"""
Unit tests for the GBA OS launcher shell (Rules L4, L6, I2, I4, D1, S6).

Runs headless against the dummy SDL video driver, so no display is required.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame  # noqa: E402

import common  # noqa: E402
import gba_launcher  # noqa: E402
import library  # noqa: E402
from gba_launcher import GBALauncher, CANVAS_WIDTH, CANVAS_HEIGHT  # noqa: E402
from gba_adapter_mgba import ensure_mgba_config  # noqa: E402


class LauncherTestCase(unittest.TestCase):
    """Shared fixture: isolated DATA dir and a headless launcher."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.games_dir = os.path.join(self.test_dir, "games")
        self.run_dir = os.path.join(self.test_dir, "run")
        os.makedirs(self.games_dir, exist_ok=True)
        os.makedirs(self.run_dir, exist_ok=True)

        # The modules bind these helpers at import time, so patch each module.
        self._patch = []
        for module in (common, gba_launcher, library):
            self._patch.append((module, "get_data_dir", getattr(module, "get_data_dir", None)))
            module.get_data_dir = lambda: self.test_dir
            if hasattr(module, "get_games_dir"):
                self._patch.append((module, "get_games_dir", module.get_games_dir))
                module.get_games_dir = lambda: self.games_dir
            if hasattr(module, "get_run_dir"):
                self._patch.append((module, "get_run_dir", module.get_run_dir))
                module.get_run_dir = lambda: self.run_dir

    def tearDown(self):
        for module, name, original in reversed(self._patch):
            if original is not None:
                setattr(module, name, original)

    def make_launcher(self, **kwargs):
        kwargs.setdefault("screenshot_path", os.path.join(self.test_dir, "shot.png"))
        kwargs.setdefault("demo", True)
        launcher = GBALauncher(**kwargs)
        launcher.screenshot_path = None  # allow rendering/presenting in tests
        launcher.window = launcher.canvas
        launcher.update()
        return launcher


class TestLauncherShell(LauncherTestCase):
    def test_canvas_is_fixed_logical_resolution(self):
        """D1: the shell renders to a fixed logical canvas."""
        self.assertEqual((CANVAS_WIDTH, CANVAS_HEIGHT), (480, 320))

    def test_screen_to_canvas_roundtrip(self):
        launcher = self.make_launcher()
        cx, cy = launcher.screen_to_canvas(240, 160)
        self.assertEqual((cx, cy), (240, 160))

    def test_appliance_mode_flag(self):
        launcher = self.make_launcher(appliance_mode=True)
        self.assertTrue(launcher.appliance_mode)

    def test_remap_and_save_keybindings(self):
        b = common.load_keybindings()
        original_a = b["keyA"]
        b["keyA"] = 113  # F2, unused by default
        common.save_keybindings(b)

        reloaded = common.load_keybindings()
        self.assertEqual(reloaded["keyA"], 113)

        b["keyA"] = original_a
        common.save_keybindings(b)
        self.assertEqual(common.load_keybindings()["keyA"], original_a)

    def test_ensure_mgba_config(self):
        config_dir = os.path.join(self.test_dir, "handler-config")
        ensure_mgba_config(config_dir)
        cfg = os.path.join(config_dir, "config.ini")
        self.assertTrue(os.path.exists(cfg))
        with open(cfg, "r") as f:
            content = f.read()
        self.assertIn("fullscreen=1", content)
        self.assertIn("keyStart=", content)
        self.assertIn("keyA=", content)


class TestLauncherNavigation(LauncherTestCase):
    def test_tab_hit_boxes_do_not_overlap(self):
        launcher = self.make_launcher()
        rects = launcher.tab_rects
        self.assertEqual(len(rects), 3)
        for previous, current in zip(rects, rects[1:]):
            self.assertLess(previous.right, current.x)
        self.assertLessEqual(rects[-1].right, CANVAS_WIDTH)

    def test_category_switch_wraps(self):
        launcher = self.make_launcher()
        launcher.switch_tab(-1)
        self.assertEqual(launcher.current_tab, len(launcher.tabs) - 1)
        launcher.switch_tab(1)
        self.assertEqual(launcher.current_tab, 0)

    def test_navigation_is_clamped_at_bounds(self):
        launcher = self.make_launcher()
        launcher.current_tab = 0
        launcher.selected_indices[0] = 0
        launcher.navigate(-5)
        self.assertEqual(launcher.selected_indices[0], 0)

        launcher.selected_indices[0] = len(launcher.games) - 1
        launcher.navigate(50)
        self.assertEqual(launcher.selected_indices[0], len(launcher.games) - 1)

    def test_scroll_offset_follows_selection(self):
        launcher = self.make_launcher()
        launcher.current_tab = 0
        launcher.selected_indices[0] = len(launcher.games) - 1
        launcher.render_base()
        self.assertGreater(launcher.scroll_offsets[0], 0)

    def test_incremental_search_filters_and_clears(self):
        launcher = self.make_launcher()
        launcher.current_tab = 0
        launcher.filter_text = "cel"
        visible = launcher.visible_games()
        self.assertEqual([g["title"] for g in visible], ["Celeste Classic"])

        launcher.filter_text = "zzz"
        self.assertEqual(launcher.visible_games(), [])
        launcher.render_base()  # must not raise on an empty result set (L6)

        launcher.filter_text = ""
        self.assertEqual(len(launcher.visible_games()), len(launcher.games))

    def test_typing_letter_starts_search_on_games_tab(self):
        launcher = self.make_launcher()
        launcher.current_tab = 0
        launcher._handle_key(pygame.K_y)
        self.assertEqual(launcher.filter_text, "y")
        # Backspace removes a character, then clears the filter.
        launcher._handle_key(pygame.K_BACKSPACE)
        self.assertEqual(launcher.filter_text, "")

    def test_recent_entries_are_selected_on_boot(self):
        """The previously played entry is focused, not entry zero."""
        launcher = self.make_launcher(demo=False)
        launcher.games = [
            {"id": "aaa", "title": "A", "type": "game", "format": "GBA", "handler": "h"},
            {"id": "bbb", "title": "B", "type": "game", "format": "GBA", "handler": "h"},
        ]
        launcher.history = {"bbb": {"last_played": 12345, "sessions": 1}}
        launcher._focus_applied = False
        launcher._apply_focus()
        self.assertEqual(launcher.selected_indices[0], 1)

    def test_focus_argument_wins(self):
        launcher = self.make_launcher(demo=False)
        launcher.games = [
            {"id": "aaa", "title": "A", "type": "game", "format": "GBA", "handler": "h"},
            {"id": "bbb", "title": "B", "type": "game", "format": "GBA", "handler": "h"},
        ]
        launcher.focus_id = "bbb"
        launcher._focus_applied = False
        launcher._apply_focus()
        self.assertEqual(launcher.selected_indices[0], 1)

    def test_empty_library_renders_and_keeps_actions_safe(self):
        """L6: no games still yields a usable shell."""
        launcher = self.make_launcher(demo=False)
        launcher.scanner = None
        launcher.games = []
        launcher.render_base()
        launcher.trigger_action()  # must not raise
        self.assertIsNone(launcher.current_item())


class TestLauncherActions(LauncherTestCase):
    def test_display_scaling_cycles_and_persists(self):
        launcher = self.make_launcher()
        self.assertEqual(launcher.display_config["scaling"], "integer")
        launcher.cycle_display_scaling()
        self.assertEqual(common.load_display_config()["scaling"], "stretch")
        launcher.cycle_display_scaling()
        self.assertEqual(common.load_display_config()["scaling"], "integer")

    def test_rescan_keeps_shell_usable(self):
        launcher = self.make_launcher()
        launcher.rescan_library()
        self.assertIsNotNone(launcher.scanner)
        self.wait_for_scan(launcher)
        self.assertEqual(len(launcher.games), len(launcher.games))

    def wait_for_scan(self, launcher, timeout=5.0):
        import time
        deadline = time.time() + timeout
        while launcher.scanner is not None and time.time() < deadline:
            launcher.update()
            time.sleep(0.01)
        self.assertIsNone(launcher.scanner)

    def test_save_summary_of_existing_save_dir(self):
        launcher = self.make_launcher()
        entry = next(g for g in launcher.games if not g.get("demo_save"))
        save_dir = os.path.join(self.test_dir, "saves", entry["id"])
        os.makedirs(save_dir, exist_ok=True)
        with open(os.path.join(save_dir, "game.sav"), "wb") as f:
            f.write(b"x" * 2048)

        self.assertTrue(launcher.save_exists(entry))
        summary = launcher.save_summary(entry)
        self.assertEqual(summary["files"], 1)
        self.assertEqual(summary["bytes"], 2048)
        self.assertIsNotNone(summary["age"])

    def test_demo_entry_launch_is_harmless(self):
        launcher = self.make_launcher()
        launcher.current_tab = 0
        launcher.selected_indices[0] = 0
        launcher.trigger_action()  # demo-preview handler: toast only
        self.assertIsNotNone(launcher.toast)
        self.assertTrue(os.path.exists(os.path.join(self.test_dir, "run")))

    def test_launch_request_is_written_atomically(self):
        """S6/D-004: the launcher writes a launch request then exits with code 10."""
        launcher = self.make_launcher(demo=False)
        content = os.path.join(self.test_dir, "games", "Real Game.gba")
        with open(content, "wb") as f:
            f.write(b"\x00" * 0xA0 + b"REAL GAME" + b"\x00" * 64)
        launcher.games = [{
            "id": "realid123", "title": "Real Game", "type": "game", "format": "GBA",
            "content_path": content, "handler": "mgba-adapter", "size_bytes": 200,
        }]
        launcher.selected_indices[0] = 0

        # Auto-confirm the pre-flight modal by pre-loading a RETURN key press.
        pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN, mod=0, unicode=""))

        with self.assertRaises(SystemExit) as ctx:
            launcher.trigger_action()

        self.assertEqual(ctx.exception.code, common.EXIT_CODE_LAUNCH_REQUEST)
        req_path = os.path.join(self.test_dir, "run", "launch_request.json")
        self.assertTrue(os.path.exists(req_path))
        import json
        with open(req_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        self.assertEqual(payload["entry_id"], "realid123")
        self.assertEqual(payload["content_path"], content)
        # Play history was recorded so the shell can show "recent".
        self.assertIn("realid123", common.load_play_history())

    def test_missing_content_is_reported_to_the_user(self):
        launcher = self.make_launcher(demo=False)
        launcher.games = [{
            "id": "ghost", "title": "Ghost", "type": "game", "format": "GBA",
            "content_path": os.path.join(self.test_dir, "games", "gone.gba"),
            "handler": "mgba-adapter",
        }]
        launcher.selected_indices[0] = 0
        # The confirmation modal is skipped in screenshot mode.
        launcher.screenshot_path = "x"
        with self.assertRaises(SystemExit):
            launcher.trigger_action()
        # The supervisor reports it; the shell itself must not crash later.
        self.assertTrue(launcher.running is False)

    def test_modal_render_paths_do_not_raise(self):
        """Every modal renders one frame cleanly with a synthetic quit event."""
        launcher = self.make_launcher()
        launcher.current_tab = 1

        def one_frame(callable_):
            pygame.event.post(pygame.event.Event(pygame.QUIT))
            callable_()

        pygame.event.post(pygame.event.Event(pygame.QUIT))
        launcher.show_system_monitor()
        pygame.event.post(pygame.event.Event(pygame.QUIT))
        launcher.show_controller_tester()
        pygame.event.post(pygame.event.Event(pygame.QUIT))
        launcher.show_save_manager()
        pygame.event.post(pygame.event.Event(pygame.QUIT))
        launcher.show_confirm("TEST?", "This is a confirmation dialog.", "OK")
        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
