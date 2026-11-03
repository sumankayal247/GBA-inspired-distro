"""Scaling regression: every popup must fit and render at any display mode.

The whole UI is expressed in design units against a 320-unit reference
height. These tests render both the handler-side in-game overlays and the
launcher's modal popups at small handheld panels, 1080p and 4K, checking that
panels stay on screen, cells stay positive, and nothing throws.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame  # noqa: E402
from unittest import mock  # noqa: E402

import config as settings_store  # noqa: E402
from common import DEFAULT_KEYBINDINGS  # noqa: E402
from gba_ingame import (  # noqa: E402
    InGameBackend, InGameSession, IndicatorOverlayModel,
)
from gba_ingame_ui import InGameOverlayRenderer  # noqa: E402
from gba_launcher import (  # noqa: E402
    ConfirmOverlay, GameManagementOverlay, ShortcutOverlay, StrongConfirmOverlay,
)
from game_data import CheatStore, SaveStateStore  # noqa: E402
from theme import Theme  # noqa: E402

GAME_A = "44636205c3e1a1b2"
DEFAULTS = settings_store.load_shortcuts(dict(settings_store.DEFAULT_SETTINGS))
SIZES = ((320, 240), (480, 320), (640, 480), (800, 600), (1024, 768),
         (1280, 720), (1920, 1080), (2560, 1440), (3840, 2160))


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

    def set_speed(self, multiplier, fps_target):
        return False


class FakeShell:
    """Minimum shell surface the launcher Overlay base class needs."""

    def __init__(self, size, rom_path):
        self.surface = pygame.Surface(size)
        self.ui_scale = settings_store.ui_scale_for(size[1])
        self.design_w = size[0] / self.ui_scale
        self.design_h = size[1] / self.ui_scale
        self.theme = Theme("indigo", self.ui_scale)
        self.appliance_mode = False
        self.overlays = []
        self.settings = dict(settings_store.DEFAULT_SETTINGS)
        self.keybindings = dict(DEFAULT_KEYBINDINGS)
        self.games = [
            {"id": GAME_A, "title": "A Very Long Game Title For Wrapping Checks",
             "type": "game", "content_path": rom_path},
            {"id": "80a88361e29f4a77", "title": "Second Cartridge",
             "type": "game", "content_path": rom_path},
        ]
        self.toast_text = ""

    def dim_background(self):
        self.surface.fill((10, 8, 22))

    def pop_overlay(self, overlay):
        if overlay in self.overlays:
            self.overlays.remove(overlay)

    def push_overlay(self, overlay):
        self.overlays.append(overlay)

    def toast(self, *args, **kwargs):
        pass


class OverlayScalingTestCase(unittest.TestCase):
    def setUp(self):
        pygame.init()
        pygame.font.init()
        self.root = tempfile.mkdtemp()
        self.rom = os.path.join(self.root, "game.gba")
        with open(self.rom, "wb") as f:
            f.write(b"ROM")
        self.cheats = CheatStore(GAME_A, self.rom, data_dir=self.root)
        self.cheats.add_cheat("A Very Long Cheat Name That Must Be Clipped",
                              "0123456789ABCDEF", enabled=True)
        self.states = SaveStateStore(GAME_A, data_dir=self.root)
        thumb = os.path.join(self.root, "thumb.png")
        pygame.image.save(pygame.Surface((64, 48)), thumb)
        self.states.save(2, b"MGBA_NATIVE_STATE", thumb, timestamp=1793698246.0)

    def tearDown(self):
        pygame.display.quit()
        shutil.rmtree(self.root, ignore_errors=True)

    def _assert_on_screen(self, surface, rect, label):
        bounds = surface.get_rect()
        self.assertTrue(rect.width > 0 and rect.height > 0, label)
        self.assertTrue(bounds.contains(rect),
                        f"{label}: {rect} escapes {bounds}")

    # ------------------------------------------------------------ in-game UI
    def test_in_game_overlays_render_at_every_display_size(self):
        for size in SIZES:
            with self.subTest(size=size):
                session = InGameSession(GAME_A, DEFAULTS, self.cheats,
                                        self.states, FakeBackend())
                renderer = InGameOverlayRenderer(session, size)
                self.assertTrue(renderer.open())
                try:
                    self.assertEqual(renderer.surface.get_size(), size)
                    self.assertGreaterEqual(renderer.theme.font("body").get_height(), 8)
                    self.assertGreater(renderer.theme.px(1), 0)
                    self._assert_on_screen(renderer.surface,
                                           renderer.panel_rect(), "panel")

                    # Cheats list (long name + ENABLED chip)
                    session.open_overlay("cheats")
                    renderer.draw()

                    # 3x3 grid with a real thumbnail + timestamp
                    session.close_overlay()
                    session.open_overlay("save_states")
                    renderer.draw()
                    x0, y0, cell_w, cell_h, gap = renderer._slot_geometry()
                    self.assertGreater(cell_w, 0)
                    self.assertGreater(cell_h, 26)   # thumbnail stays positive
                    for row in range(3):
                        for col in range(3):
                            cell = renderer.theme.rect(
                                x0 + col * (cell_w + gap),
                                y0 + row * (cell_h + gap), cell_w, cell_h)
                            self._assert_on_screen(renderer.surface, cell,
                                                   f"slot {row * 3 + col + 1}")

                    # Action + confirmation popups inside the grid popup
                    model = session.overlay
                    model.index = 1                  # occupied slot 2
                    model.activate()
                    self.assertEqual(model.mode, "actions")
                    renderer.draw()
                    model.choose_action(1)
                    self.assertEqual(model.mode, "confirm")
                    renderer.draw()
                    model.cancel()
                    model.cancel()
                    session.close_overlay()

                    # Badge-only speed overlay (no panel)
                    session.overlay = IndicatorOverlayModel(
                        "2x", 1.0, on_close=session._overlay_closed)
                    renderer.draw()
                    session.close_overlay()
                finally:
                    renderer.close()

    def test_fullscreen_uses_the_real_desktop_mode_not_the_fallback(self):
        renderer = InGameOverlayRenderer(
            InGameSession(GAME_A, DEFAULTS, self.cheats, self.states,
                          FakeBackend()), (960, 640))
        info = mock.Mock(current_w=1920, current_h=1080)
        with mock.patch.object(pygame.display, "Info", return_value=info):
            self.assertEqual(renderer._fullscreen_size((960, 640)),
                             (1920, 1080))
        renderer._apply_size((1920, 1080))
        self.assertEqual(renderer.size, (1920, 1080))
        self.assertAlmostEqual(renderer.theme.scale, 3.375)
        self.assertEqual(renderer.design_h, 320)

    def test_fullscreen_size_falls_back_when_sdl_cannot_report_a_mode(self):
        renderer = InGameOverlayRenderer(
            InGameSession(GAME_A, DEFAULTS, self.cheats, self.states,
                          FakeBackend()), (800, 600))
        info = mock.Mock(current_w=0, current_h=0)
        with mock.patch.object(pygame.display, "Info", return_value=info):
            self.assertEqual(renderer._fullscreen_size((800, 600)), (800, 600))

    # ------------------------------------------------------------ launcher UI
    def test_launcher_popups_are_centred_and_inside_every_display_size(self):
        for size in SIZES:
            with self.subTest(size=size):
                shell = FakeShell(size, self.rom)
                overlays = [
                    ShortcutOverlay(shell),
                    ConfirmOverlay("DELETE ALL?", "This cannot be undone.",
                                   "DELETE", True, None),
                    StrongConfirmOverlay("LAST CHANCE", "Hold A to delete."),
                    GameManagementOverlay(shell),
                ]
                for overlay in overlays:
                    rect = overlay.rect(shell)
                    self._assert_on_screen(shell.surface, rect, overlay.title)
                    # Theme.px() rounds design units to whole pixels, so allow
                    # a 2px rounding tolerance at tiny scales.
                    self.assertAlmostEqual(rect.centery, size[1] / 2, delta=2.0)
                    content = overlay.content_rect(shell)
                    self.assertGreater(content.width, 0)
                    self.assertGreater(content.height, 0)
                    overlay.draw(shell)          # no exception at this scale

    def test_popup_height_never_exceeds_the_design_canvas(self):
        # ui_scale_for() clamps at 6.0 above 1920px, so design_h > 320.
        shell = FakeShell((3840, 2160), self.rom)
        self.assertGreater(shell.design_h, 320)
        for overlay in (ShortcutOverlay(shell), GameManagementOverlay(shell),
                        StrongConfirmOverlay("X", "Y")):
            rect = overlay.rect(shell)
            self.assertLessEqual(rect.bottom / shell.ui_scale, shell.design_h)
            self.assertGreaterEqual(rect.top, 0)


if __name__ == "__main__":
    unittest.main()
