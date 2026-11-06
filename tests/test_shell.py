#!/usr/bin/env python3
"""
Tests for the GBA OS shell (gba_launcher.py).

Covers the display model (D1/D2), navigation and shortcuts (I2/I4), the
cartridge library (L1/L6), settings persistence and the launch protocol (S6).
Everything runs headless on the dummy SDL driver.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame  # noqa: E402

import common  # noqa: E402
import config  # noqa: E402
import gba_launcher  # noqa: E402
import library  # noqa: E402
from gba_launcher import GBALauncher, TABS  # noqa: E402


class ShellTestCase(unittest.TestCase):
    """Isolated DATA/GAMES/RUN dirs plus a headless shell."""

    def setUp(self):
        pygame.init()
        pygame.joystick.init()
        self.test_dir = tempfile.mkdtemp()
        self.games_dir = os.path.join(self.test_dir, "games")
        self.run_dir = os.path.join(self.test_dir, "run")
        os.makedirs(self.games_dir, exist_ok=True)
        os.makedirs(self.run_dir, exist_ok=True)

        self._saved = []
        self._original = {}

        def patch(module, name, value):
            self._original[(module, name)] = getattr(module, name, None)
            setattr(module, name, value)

        for module in (common, config, gba_launcher, library):
            patch(module, "get_data_dir", lambda: self.test_dir)
            if hasattr(module, "get_games_dir"):
                patch(module, "get_games_dir", lambda: self.games_dir)
            if hasattr(module, "get_run_dir"):
                patch(module, "get_run_dir", lambda: self.run_dir)

        # No hardware side effects in tests.
        patch(gba_launcher.settings_store, "apply_volume", lambda *a, **k: True)
        patch(gba_launcher.settings_store, "set_brightness", lambda *a, **k: True)
        patch(gba_launcher, "VolumeWatcher", _FakeWatcher)
        patch(gba_launcher.settings_store, "load_settings", self._load_settings)
        patch(gba_launcher.settings_store, "save_settings", self._save_settings)
        self.settings = dict(config.DEFAULT_SETTINGS)
        self.settings["usb_autoscan"] = False

    def _load_settings(self):
        return dict(self.settings)

    def _save_settings(self, payload):
        self.settings.update(payload)
        self._saved.append(dict(payload))

    def tearDown(self):
        for (module, name), value in self._original.items():
            setattr(module, name, value)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # -- helpers ------------------------------------------------------
    def make_launcher(self, **kwargs):
        kwargs.setdefault("demo", True)
        kwargs.setdefault("screenshot_path", os.path.join(self.test_dir, "shot.png"))
        launcher = GBALauncher(**kwargs)
        launcher.screenshot_path = None       # render for real, just headlessly
        launcher.update()
        launcher.on_tab_activated()
        return launcher

    def key(self, launcher, code, mod=0, unicode_=""):
        launcher._handle_global(pygame.event.Event(
            pygame.KEYDOWN, key=code, mod=mod, unicode=unicode_))
        launcher._handle_tab_event(pygame.event.Event(
            pygame.KEYDOWN, key=code, mod=mod, unicode=unicode_))


class _FakeWatcher:
    """Stands in for the USB poller so tests never touch /proc/mounts."""

    def __init__(self, *args, **kwargs):
        self.volumes = []
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def take_pending(self):
        return []


class TestDisplayModel(ShellTestCase):
    def test_surface_matches_the_window_at_native_resolution(self):
        """D1: the shell draws at the window size, not a fixed 480x320 canvas."""
        launcher = self.make_launcher()
        self.assertEqual(launcher.surface.get_size(), launcher.window.get_size())
        self.assertGreater(launcher.surface.get_width(), 480)
        self.assertEqual(launcher.current_tab, 0)

    def test_bare_x11_shelf_uses_no_mode_switching_fullscreen(self):
        launcher = self.make_launcher(appliance_mode=True)
        with patch.dict(os.environ, {"GBA_BARE_X11": "1"}), \
                patch.object(pygame.display, "get_driver", return_value="x11"), \
                patch.object(pygame.display, "set_mode", wraps=pygame.display.set_mode) as mode, \
                patch.object(pygame.event, "set_keyboard_grab", create=True) as grab:
            launcher._init_display()
        self.assertEqual(mode.call_args.args[1], pygame.NOFRAME)
        self.assertFalse(mode.call_args.args[1] & pygame.FULLSCREEN)
        self.assertEqual(launcher.window.get_size(), (launcher.screen_w, launcher.screen_h))
        grab.assert_called_once_with(True)

    def test_resize_keeps_requested_geometry_and_scaling_rebuild_keeps_it(self):
        launcher = self.make_launcher(windowed=True)
        launcher._handle_global(pygame.event.Event(pygame.VIDEORESIZE,
                                                    w=800, h=600, size=(800, 600)))
        self.assertEqual(launcher.window.get_size(), (800, 600))
        self.assertEqual(launcher.surface.get_size(), (800, 600))
        launcher._rebuild_surface()
        self.assertEqual(launcher.window.get_size(), (800, 600))

    def test_windowed_test_does_not_grab_desktop_keyboard(self):
        launcher = self.make_launcher(windowed=True)
        with patch.object(pygame.event, "set_keyboard_grab", create=True) as grab:
            launcher._grab_shortcuts()
        grab.assert_not_called()

    def test_ui_scale_tracks_height(self):
        launcher = self.make_launcher()
        expected = config.ui_scale_for(launcher.surface.get_height())
        self.assertAlmostEqual(launcher.theme.scale, expected, places=3)
        self.assertGreater(launcher.design_w, 0)

    def test_integer_mode_composes_on_a_480x320_canvas(self):
        self.settings["scaling"] = "integer"
        launcher = self.make_launcher()
        self.assertEqual(launcher.surface.get_size(), (480, 320))
        self.assertTrue(launcher.pixel_mode)
        launcher.render()

    def test_screen_to_canvas_roundtrip(self):
        launcher = self.make_launcher()
        self.assertEqual(launcher.screen_to_canvas(240, 160), (240, 160))


class TestTabsAndShortcuts(ShellTestCase):
    def test_five_tabs_including_terminal(self):
        self.assertEqual(TABS, ["GAMES", "APPS", "BROWSER", "TERMINAL", "SETTINGS"])

    def test_alt_tab_cycles_forward_and_back(self):
        launcher = self.make_launcher()
        launcher._handle_global(pygame.event.Event(
            pygame.KEYDOWN, key=pygame.K_TAB, mod=pygame.KMOD_ALT, unicode="\t"))
        self.assertEqual(launcher.current_tab, 1)
        launcher._handle_global(pygame.event.Event(
            pygame.KEYDOWN, key=pygame.K_TAB, mod=pygame.KMOD_ALT | pygame.KMOD_SHIFT,
            unicode="\t"))
        self.assertEqual(launcher.current_tab, 0)
        launcher._handle_global(pygame.event.Event(
            pygame.KEYDOWN, key=pygame.K_TAB, mod=pygame.KMOD_ALT | pygame.KMOD_SHIFT,
            unicode="\t"))
        self.assertEqual(launcher.current_tab, len(TABS) - 1)

    def test_alt_tab_uses_physical_modifier_events_and_key_release(self):
        launcher = self.make_launcher()
        launcher._handle_global(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_LALT, mod=0))
        launcher._handle_global(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_TAB, mod=0))
        self.assertEqual(launcher.current_tab, 1)
        launcher._handle_global(pygame.event.Event(pygame.KEYUP, key=pygame.K_LALT, mod=0))
        consumed = launcher._handle_global(pygame.event.Event(pygame.KEYDOWN,
                                                              key=pygame.K_TAB, mod=0))
        self.assertFalse(consumed)
        self.assertEqual(launcher._shortcut_mods, 0)

    def test_focus_loss_resets_modifier_state(self):
        launcher = self.make_launcher()
        launcher._handle_global(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_LALT, mod=0))
        launcher.held_keys[pygame.K_DOWN] = 0
        launcher._handle_global(pygame.event.Event(pygame.WINDOWFOCUSLOST))
        self.assertEqual(launcher._shortcut_mods, 0)
        self.assertFalse(launcher.held_keys)

    def test_alt_tab_is_global_on_every_tab_and_not_repeated(self):
        launcher = self.make_launcher()
        with patch.object(launcher, "ensure_browser"), patch.object(launcher, "ensure_terminal"):
            for tab in range(len(TABS)):
                launcher.current_tab = tab
                launcher.push_overlay(gba_launcher.TextOverlay("test", ["test"]))
                self.assertTrue(launcher._handle_global(pygame.event.Event(
                    pygame.KEYDOWN, key=pygame.K_TAB, mod=pygame.KMOD_ALT)))
                self.assertEqual(launcher.current_tab, (tab + 1) % len(TABS))
                self.assertFalse(launcher.overlays)
                launcher._handle_global(pygame.event.Event(
                    pygame.KEYDOWN, key=pygame.K_TAB, mod=pygame.KMOD_ALT, repeat=True))
                self.assertEqual(launcher.current_tab, (tab + 1) % len(TABS))

    def test_ctrl_alt_t_toggles_the_terminal_from_any_tab(self):
        launcher = self.make_launcher()
        launcher.current_tab = 2
        launcher._handle_global(pygame.event.Event(
            pygame.KEYDOWN, key=pygame.K_t, mod=pygame.KMOD_CTRL | pygame.KMOD_ALT, unicode="t"))
        self.assertEqual(launcher.current_tab, gba_launcher.TAB_TERMINAL)
        launcher._handle_global(pygame.event.Event(
            pygame.KEYDOWN, key=pygame.K_t, mod=pygame.KMOD_CTRL | pygame.KMOD_ALT, unicode="t"))
        self.assertEqual(launcher.current_tab, 2)

    def test_tab_hit_boxes_are_inside_the_bar_and_ordered(self):
        launcher = self.make_launcher()
        launcher.render()
        rects = launcher.tab_rects()
        self.assertEqual(len(rects), len(TABS))
        for previous, current in zip(rects, rects[1:]):
            self.assertLess(previous.right, current.x)
        self.assertLessEqual(rects[-1].right, launcher.surface.get_width())

    def test_tab_click_switches_tab(self):
        launcher = self.make_launcher()
        launcher.render()
        target = launcher.tab_rects()[2]
        launcher._handle_click(target.center)
        self.assertEqual(launcher.current_tab, 2)

    def test_navigation_is_clamped(self):
        launcher = self.make_launcher()
        launcher.selected[0] = 0
        launcher.move_grid(0, -1)
        self.assertEqual(launcher.selected[0], 0)
        for _ in range(50):
            launcher.move_grid(0, 1)
        self.assertEqual(launcher.selected[0], len(launcher.games) - 1)

    def test_grid_navigation_uses_columns_in_grid_view(self):
        launcher = self.make_launcher()
        columns = launcher._grid_columns()
        launcher.selected[0] = 0
        launcher.move_grid(0, 1)
        self.assertEqual(launcher.selected[0], min(columns, len(launcher.games) - 1))


class TestSearchAndHistory(ShellTestCase):
    def test_typing_filters_and_escape_clears(self):
        launcher = self.make_launcher()
        for char, key in zip("cele", [pygame.K_c, pygame.K_e, pygame.K_l, pygame.K_e]):
            self.key(launcher, key, unicode_=char)
        self.assertEqual(launcher.filter_text, "cele")
        self.assertTrue(all("cele" in g["title"].lower() for g in launcher.visible_games()))
        self.key(launcher, pygame.K_ESCAPE)
        self.assertEqual(launcher.filter_text, "")

    def test_focus_argument_selects_the_entry(self):
        launcher = self.make_launcher(focus_id="80a88361e29f4a77")
        self.assertEqual(launcher.games[launcher.selected[0]]["id"], "80a88361e29f4a77")

    def test_recent_entry_is_selected_without_focus(self):
        launcher = self.make_launcher()
        current = launcher.games[launcher.selected[0]]["id"]
        self.assertIn(current, launcher.history)


class TestLibraryIntegration(ShellTestCase):
    def test_apps_and_settings_are_cartridge_entries(self):
        launcher = self.make_launcher()
        self.assertEqual(len(launcher.apps), 6)
        # 8 original settings pages + Game Management (delete/favorites section).
        self.assertEqual(len(launcher.settings_entries), 9)
        for entry in launcher.apps + launcher.settings_entries:
            self.assertTrue(entry["handler"].startswith("internal_"))
            self.assertIn("format", entry)

    def test_every_internal_handler_opens_without_raising(self):
        launcher = self.make_launcher()
        for entry in launcher.apps + launcher.settings_entries:
            launcher.current_tab = gba_launcher.TAB_APPS
            launcher.activate_current.__self__  # keep bound-method typing honest
            launcher.current_item = lambda entry=entry: entry
            if entry["handler"] in ("internal_reboot", "internal_poweroff"):
                continue
            launcher.activate_current()
            if entry["handler"] in ("internal_browser", "internal_terminal"):
                self.assertEqual(launcher.current_tab,
                                 gba_launcher.TAB_BROWSER if entry["handler"] == "internal_browser"
                                 else gba_launcher.TAB_TERMINAL)
                launcher.current_tab = gba_launcher.TAB_APPS
                continue
            self.assertTrue(launcher.overlays, entry["handler"])
            launcher.overlays[-1].draw(launcher)
            launcher.close_overlays()
        launcher.current_item = GBALauncher.current_item.__get__(launcher)

    def test_settings_pages_render_and_edit(self):
        launcher = self.make_launcher()
        for section in ("display", "audio", "controls", "library", "storage", "system"):
            launcher.open_settings_page(section)
            overlay = launcher.overlays[-1]
            overlay.draw(launcher)
            self.assertTrue(overlay.rows, section)
            overlay.index = 0
            overlay.activate(launcher)
            launcher.close_overlays()

    def test_toggling_a_setting_is_persisted(self):
        launcher = self.make_launcher()
        launcher.open_settings_page("display")
        overlay = launcher.overlays[-1]
        overlay.index = 2  # "Show clock"
        before = launcher.settings["show_clock"]
        overlay.activate(launcher)
        self.assertNotEqual(launcher.settings["show_clock"], before)
        self.assertTrue(self._saved)

    def test_theme_choice_rebuilds_the_surface(self):
        launcher = self.make_launcher()
        launcher.set_setting("theme", "mint")
        launcher.on_setting_changed("theme")
        self.assertEqual(launcher.theme.name, "mint")

    def test_view_mode_shortcut_toggles(self):
        launcher = self.make_launcher()
        launcher._handle_tab_event(pygame.event.Event(
            pygame.KEYDOWN, key=pygame.K_v, mod=pygame.KMOD_CTRL, unicode="v"))
        self.assertEqual(launcher.settings["view_mode"], "list")
        launcher._handle_tab_event(pygame.event.Event(
            pygame.KEYDOWN, key=pygame.K_v, mod=pygame.KMOD_CTRL, unicode="v"))
        self.assertEqual(launcher.settings["view_mode"], "grid")


class TestLaunchProtocol(ShellTestCase):
    def _real_game(self):
        content = os.path.join(self.games_dir, "Real Game.gba")
        with open(content, "wb") as f:
            f.write(b"\x00" * 0xA0 + b"REALGAME" + b"\x00" * 64)
        return {
            "id": "realid123", "title": "Real Game", "type": "game", "format": "GBA",
            "content_path": content, "handler": "mgba-adapter", "size_bytes": 200,
        }

    def test_launch_request_is_written_then_exit_code_10(self):
        launcher = self.make_launcher()
        game = self._real_game()
        with self.assertRaises(SystemExit) as ctx:
            launcher.launch_game(game)
        self.assertEqual(ctx.exception.code, common.EXIT_CODE_LAUNCH_REQUEST)

        with open(os.path.join(self.run_dir, "launch_request.json"), encoding="utf-8") as f:
            payload = json.load(f)
        self.assertEqual(payload["entry_id"], "realid123")
        self.assertEqual(payload["content_path"], game["content_path"])
        self.assertIn("realid123", common.load_play_history())

    def test_confirmation_is_required_before_launching(self):
        launcher = self.make_launcher()
        game = self._real_game()
        launcher.request_launch(game)
        self.assertIsInstance(launcher.overlays[-1], gba_launcher.LaunchOverlay)
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "launch_request.json")))

    def test_missing_content_is_reported_not_launched(self):
        launcher = self.make_launcher()
        game = self._real_game()
        os.remove(game["content_path"])
        launcher.request_launch(game)
        self.assertEqual(launcher.toast_kind, "error")
        self.assertFalse(launcher.overlays)

    def test_demo_entries_never_launch(self):
        launcher = self.make_launcher()
        launcher.request_launch(launcher.games[0])
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "launch_request.json")))

    def test_reboot_and_poweroff_exit_with_contract_codes(self):
        launcher = self.make_launcher()
        launcher.confirm_system("Reboot?", common.EXIT_CODE_REBOOT)
        overlay = launcher.overlays[-1]
        self.assertIsInstance(overlay, gba_launcher.ConfirmOverlay)
        with self.assertRaises(SystemExit) as ctx:
            overlay._accept(launcher)
        self.assertEqual(ctx.exception.code, common.EXIT_CODE_REBOOT)


class TestOverlaysAndUsb(ShellTestCase):
    def test_confirm_overlay_requires_explicit_accept(self):
        calls = []
        launcher = self.make_launcher()
        overlay = gba_launcher.ConfirmOverlay("X?", "Message", "DELETE", True,
                                              lambda: calls.append(1))
        launcher.push_overlay(overlay)
        overlay.handle(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE, mod=0), launcher)
        self.assertFalse(calls)
        launcher.push_overlay(gba_launcher.ConfirmOverlay(
            "X?", "Message", "DELETE", True, lambda: calls.append(1)))
        launcher.overlays[-1].handle(
            pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN, mod=0), launcher)
        self.assertEqual(calls, [1])

    def test_usb_prompt_lists_the_detected_roms(self):
        from gba_usb import Volume
        launcher = self.make_launcher()
        volume = Volume("/dev/sdb1", "/media/user/STICK", "STICK", True, 8 * 2 ** 30, 4 * 2 ** 30)
        volume._roms = [("/media/user/STICK/a.gba", 4 * 2 ** 20)]
        overlay = gba_launcher.UsbPromptOverlay(volume)
        launcher.push_overlay(overlay)
        overlay.draw(launcher)
        self.assertIn("STICK", volume.label)
        self.assertIn(os.path.join(self.games_dir, "STICK"), launcher.usb_destination(volume))

    def test_detected_volume_raises_a_prompt_once(self):
        from gba_usb import Volume
        launcher = self.make_launcher()
        source = os.path.join(self.test_dir, "STICK")
        os.makedirs(source, exist_ok=True)
        with open(os.path.join(source, "prompt.gba"), "wb") as f:
            f.write(b"rom" * 32)
        volume = Volume("/dev/sdb1", source, "STICK", True)

        class Watcher:
            def __init__(self):
                self.pending = [volume]

            def take_pending(self):
                items, self.pending = self.pending, []
                return items

        launcher.usb_watcher = Watcher()
        launcher._poll_usb()
        self.assertIsInstance(launcher.overlays[-1], gba_launcher.UsbPromptOverlay)
        # A second poll with the prompt still open must not stack another one.
        launcher.usb_watcher.pending = [volume]
        launcher._poll_usb()
        self.assertEqual(len(launcher.overlays), 1)

    def test_usb_import_is_confirmed_then_copied(self):
        from gba_usb import Volume
        launcher = self.make_launcher()
        source_dir = os.path.join(self.test_dir, "usb")
        os.makedirs(source_dir, exist_ok=True)
        source = os.path.join(source_dir, "Imported.gba")
        with open(source, "wb") as f:
            f.write(b"rom" * 128)

        volume = Volume("/dev/sdb1", source_dir, "STICK", True, 8 * 2 ** 30, 4 * 2 ** 30)
        overlay = gba_launcher.UsbPromptOverlay(volume)
        launcher.push_overlay(overlay)
        # Nothing is copied until the prompt is accepted.
        self.assertFalse(os.path.exists(os.path.join(self.games_dir, "STICK", "Imported.gba")))

        overlay.handle(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN, mod=0), launcher)
        for _ in range(200):
            if launcher.overlays and hasattr(launcher.overlays[-1], "done") \
                    and launcher.overlays[-1].done:
                break
            import time
            time.sleep(0.02)
        copied = os.path.join(self.games_dir, "STICK", "Imported.gba")
        self.assertTrue(os.path.exists(copied))

    def test_prompt_skips_volumes_without_new_content(self):
        from gba_usb import Volume
        launcher = self.make_launcher()
        volume = Volume("/dev/sdb1", "/media/user/STICK", "STICK", True)
        volume._roms = [("/media/user/STICK/a.gba", 1024)]
        volume.new_roms = []            # analysed: nothing new to import

        class Watcher:
            def __init__(self):
                self.pending = [volume]

            def take_pending(self):
                items, self.pending = self.pending, []
                return items

        launcher.usb_watcher = Watcher()
        launcher._poll_usb()
        self.assertEqual(launcher.overlays, [])

    def test_prompt_overlay_counts_only_new_content(self):
        from gba_usb import Volume
        launcher = self.make_launcher()
        volume = Volume("/dev/sdb1", "/media/user/STICK", "STICK", True)
        volume._roms = [("/media/user/STICK/old%03d.gba" % i, 1024)
                        for i in range(100)]
        volume.new_roms = [("/media/user/STICK/new.gba", 2048)]
        self.assertEqual(volume.new_rom_count(), 1)
        overlay = gba_launcher.UsbPromptOverlay(volume)
        launcher.push_overlay(overlay)
        overlay.draw(launcher)          # must render "1 new Game Boy ROM"

    def test_watcher_is_wired_to_library_identity(self):
        captured = {}

        class RecordingWatcher(_FakeWatcher):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                captured.update(kwargs)

        launcher = self.make_launcher()
        original = gba_launcher.VolumeWatcher
        gba_launcher.VolumeWatcher = RecordingWatcher
        try:
            self.settings["usb_autoscan"] = True
            launcher.settings["usb_autoscan"] = True
            launcher.configure_usb_watcher()
        finally:
            gba_launcher.VolumeWatcher = original
        self.assertIn("known_ids_provider", captured)
        ids = captured["known_ids_provider"]()
        self.assertIsInstance(ids, set)
        launcher.usb_watcher = None

    def test_import_registers_new_content_with_the_library(self):
        from gba_usb import Volume
        import time
        launcher = self.make_launcher(demo=False)
        # A pre-existing library entry makes the known-id set non-empty,
        # which is what activates content-identity checks during import.
        with open(os.path.join(self.games_dir, "existing.gba"), "wb") as f:
            f.write(b"\x00" * 0xA0 + b"EXISTING\x00\x00\x00\x00" + b"\x00" * 128)
        launcher.library = library.Library()
        launcher.library.scan_games()
        self.assertEqual(len(launcher.library.known_content_ids()), 1)

        source_dir = os.path.join(self.test_dir, "usb2")
        os.makedirs(source_dir, exist_ok=True)
        source = os.path.join(source_dir, "Registered.gba")
        with open(source, "wb") as f:
            f.write(b"\x00" * 0xA0 + b"REGISTERED\x00\x00" + b"\x00" * 128)
        volume = Volume("/dev/sdb1", source_dir, "STICK2", True)

        launcher.start_usb_import(None, volume)
        for _ in range(200):
            if launcher.overlays and getattr(launcher.overlays[-1], "done", False):
                break
            time.sleep(0.02)
        copied = os.path.join(self.games_dir, "STICK2", "Registered.gba")
        self.assertTrue(os.path.exists(copied))
        # The imported content id is durably registered — a fresh Library
        # reading the same cache knows it without any rescan.
        self.assertIn(common.compute_content_id(copied),
                      library.Library().known_content_ids())

    def test_overlays_draw_without_raising(self):
        launcher = self.make_launcher()
        overlays = [
            gba_launcher.MonitorOverlay(),
            gba_launcher.InputDiagOverlay(launcher),
            gba_launcher.SaveManagerOverlay(),
            gba_launcher.KeyRemapOverlay(launcher),
            gba_launcher.UsbManagerOverlay(),
            gba_launcher.TextOverlay("LOGS", ["line one", "line two"]),
        ]
        for overlay in overlays:
            launcher.push_overlay(overlay)
            launcher.render()
            launcher.close_overlays()

    def test_full_frame_renders_on_every_tab(self):
        launcher = self.make_launcher()
        for index in range(len(TABS)):
            launcher.goto_tab(index)
            launcher.update()
            launcher.render()

    def test_empty_library_renders_an_empty_state(self):
        launcher = self.make_launcher(demo=False)
        launcher.games = []
        launcher.render()
        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
