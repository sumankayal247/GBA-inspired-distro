"""Headless rendering/input tests for the in-game overlays (pygame + Theme)."""

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

import config as settings_store  # noqa: E402
from game_data import CheatStore, SaveStateStore  # noqa: E402
from gba_ingame import (  # noqa: E402
    CheatsOverlayModel, InGameBackend, InGameSession, SaveStatesOverlayModel,
)
from gba_ingame_ui import InGameOverlayRenderer  # noqa: E402

GAME_A = "44636205c3e1a1b2"
DEFAULTS = settings_store.load_shortcuts(dict(settings_store.DEFAULT_SETTINGS))


class FakeBackend(InGameBackend):
    def __init__(self):
        self.paused = False

    def pause(self):
        self.paused = True
        return True

    def resume(self):
        self.paused = False
        return True

    def save_state(self, slot):
        return f"STATE_{slot}".encode()

    def capture_screenshot(self, slot):
        return b"PNG"


class OverlayUITestCase(unittest.TestCase):
    def setUp(self):
        pygame.init()
        pygame.font.init()
        self.root = tempfile.mkdtemp()
        self.cheats = CheatStore(GAME_A, "game.gba", data_dir=self.root)
        self.states = SaveStateStore(GAME_A, data_dir=self.root)
        self.backend = FakeBackend()
        self.session = InGameSession(GAME_A, DEFAULTS, self.cheats,
                                     self.states, self.backend)
        self.renderer = InGameOverlayRenderer(self.session, (960, 640))

    def tearDown(self):
        self.renderer.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def _event(self, kind, **kwargs):
        return pygame.event.Event(kind, **kwargs)

    def test_bare_x11_overlay_matches_screen_window_strategy(self):
        with patch.dict(os.environ, {"GBA_BARE_X11": "1", "SDL_VIDEODRIVER": "x11"}), \
                patch.object(pygame.display, "get_driver", return_value="x11"), \
                patch.object(pygame.display, "set_mode", wraps=pygame.display.set_mode) as mode:
            self.assertTrue(self.renderer.open())
            self.assertEqual(mode.call_args.args[1], pygame.NOFRAME)
            self.assertEqual(os.environ["SDL_VIDEO_WINDOW_POS"], "0,0")

    def test_fullscreen_resolution_and_theme_scaling(self):
        self.assertTrue(self.renderer.open())
        self.assertEqual(self.renderer.surface.get_size(), (960, 640))
        self.assertAlmostEqual(self.renderer.theme.scale, 2.0)
        self.assertLess(self.renderer.panel_rect().width, 960)
        self.assertGreater(self.renderer.theme.font("body").get_height(), 10)

    def test_cheats_overlay_renders_selection_and_enabled_state(self):
        self.cheats.add_cheat("Infinite Lives", "0123 4567")
        self.cheats.add_cheat("Moon Jump", "89AB CDEF", enabled=True)
        self.session.open_overlay("cheats")
        self.assertTrue(self.renderer.open())
        self.renderer.draw()
        self.assertIsInstance(self.session.overlay, CheatsOverlayModel)
        self.assertEqual(self.session.overlay.index, 0)
        # Keyboard navigation: down selects the second cheat, A toggles it.
        self.renderer.handle(self._event(pygame.KEYDOWN, key=pygame.K_DOWN))
        self.assertEqual(self.session.overlay.index, 1)
        self.renderer.handle(self._event(pygame.KEYDOWN, key=pygame.K_RETURN))
        self.assertFalse(self.session.overlay.items()[1]["enabled"])
        self.renderer.draw()
        # B closes without touching the emulator process.
        self.renderer.handle(self._event(pygame.KEYDOWN, key=pygame.K_ESCAPE))
        self.assertIsNone(self.session.overlay)
        self.assertFalse(self.backend.paused)
        self.assertFalse(self.session.emulator_terminated)

    def test_gamepad_navigates_the_cheats_overlay(self):
        self.cheats.add_cheat("One", "A")
        self.cheats.add_cheat("Two", "B")
        self.session.open_overlay("cheats")
        self.assertTrue(self.renderer.open())
        self.renderer.handle(self._event(pygame.JOYHATMOTION, value=(0, -1)))
        self.assertEqual(self.session.overlay.index, 1)
        self.renderer.handle(self._event(pygame.JOYBUTTONDOWN, button=0))
        self.assertTrue(self.session.overlay.items()[1]["enabled"])
        self.renderer.handle(self._event(pygame.JOYBUTTONDOWN, button=1))
        self.assertIsNone(self.session.overlay)

    def test_save_states_overlay_renders_nine_cards_with_thumbnails(self):
        thumbnail = os.path.join(self.root, "thumb.png")
        pygame.image.save(pygame.Surface((48, 32)), thumbnail)
        self.states.save(1, b"STATE_ONE", thumbnail)
        self.session.open_overlay("save_states")
        self.assertTrue(self.renderer.open())
        self.assertIsInstance(self.session.overlay, SaveStatesOverlayModel)
        self.assertEqual(len(self.session.overlay.slots()), 9)
        self.renderer.draw()                     # thumbnail loaded and blitted
        self.renderer._draw_thumbnail("/missing/file.png",
                                      pygame.Rect(0, 0, 40, 30))
        # Occupied slot shows the two actions; load returns to gameplay.
        self.session.overlay.activate()
        self.assertEqual(self.session.overlay.mode, "actions")
        self.renderer.draw()
        self.session.overlay.choose_action(1)   # overwrite -> confirm step
        self.assertEqual(self.session.overlay.mode, "confirm")
        self.renderer.draw()
        self.session.overlay.cancel()
        self.session.overlay.cancel()

    def test_speed_indicator_is_drawn(self):
        self.session.show_indicator("2x", seconds=10)
        self.session.open_overlay("cheats")
        self.assertTrue(self.renderer.open())
        self.renderer.draw()
        self.assertEqual(self.session.indicator, "2x")

    def test_speed_badge_overlay_shows_no_panel_and_expires(self):
        clock = [0.0]
        self.session.clock = lambda: clock[0]
        self.session.press("keyL")
        self.session.press("keyR")
        self.assertEqual(self.session.overlay.kind, "indicator")
        self.assertTrue(self.renderer.open())
        self.renderer.draw()                  # only frame + badge, no panel
        self.assertEqual(self.session.indicator, "2x")
        clock[0] = 10.0
        self.assertTrue(self.session.overlay.expired())
        self.assertTrue(self.backend.paused)
        self.session.close_overlay()
        self.assertFalse(self.backend.paused)

    def test_run_exits_when_overlay_closes_and_releases_the_display(self):
        self.session.open_overlay("cheats")
        pumped = []

        def poll():
            pumped.append(True)
            if len(pumped) == 2:
                self.session.close_overlay()

        self.renderer.poll_actions = poll
        self.renderer.run()
        self.assertIsNone(self.session.overlay)
        self.assertFalse(self.backend.paused)
        self.assertFalse(self.renderer._display_ready)
        self.assertGreaterEqual(len(pumped), 2)


if __name__ == "__main__":
    unittest.main()
