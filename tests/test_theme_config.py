#!/usr/bin/env python3
"""
Tests for the settings store (config.py) and the visual language (theme.py).

Covers D-029 (native-resolution scaling), D-030 (theme system) and the
settings round-trip that every settings page depends on.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame  # noqa: E402

import config  # noqa: E402
import theme as theme_module  # noqa: E402


class ConfigTestCase(unittest.TestCase):
    def setUp(self):
        pygame.init()
        pygame.font.init()
        self.test_dir = tempfile.mkdtemp()
        self._orig = config.get_data_dir
        config.get_data_dir = lambda: self.test_dir

    def tearDown(self):
        config.get_data_dir = self._orig
        shutil.rmtree(self.test_dir, ignore_errors=True)


class TestSettingsStore(ConfigTestCase):
    def test_defaults_are_sane(self):
        settings = config.load_settings()
        self.assertEqual(settings["scaling"], "fit")
        self.assertEqual(settings["view_mode"], "grid")
        self.assertIn(settings["theme"], theme_module.THEME_ORDER)

    def test_roundtrip_persists_changes(self):
        settings = config.load_settings()
        settings.update({"theme": "mint", "volume": 42, "show_clock": False,
                         "view_mode": "list"})
        config.save_settings(settings)

        reloaded = config.load_settings()
        self.assertEqual(reloaded["theme"], "mint")
        self.assertEqual(reloaded["volume"], 42)
        self.assertFalse(reloaded["show_clock"])
        self.assertEqual(reloaded["view_mode"], "list")

    def test_unknown_and_invalid_values_are_sanitised(self):
        os.makedirs(os.path.join(self.test_dir, "config"), exist_ok=True)
        with open(os.path.join(self.test_dir, "config", "settings.json"), "w") as f:
            json.dump({"scaling": "banana", "theme": "neon", "volume": 250,
                       "view_mode": "carousel", "nonsense": 1, "show_clock": "yes"}, f)
        settings = config.load_settings()
        self.assertEqual(settings["scaling"], "fit")
        self.assertEqual(settings["theme"], "indigo")
        self.assertEqual(settings["volume"], 100)
        self.assertEqual(settings["view_mode"], "grid")
        self.assertNotIn("nonsense", settings)
        self.assertIsInstance(settings["show_clock"], bool)

    def test_legacy_labels_map_to_modes(self):
        self.assertEqual(config.normalise_scaling("Integer Aspect (Crisp)"), "integer")
        self.assertEqual(config.normalise_scaling("Fullscreen Stretched"), "fit")
        self.assertEqual(config.normalise_scaling("stretch"), "fit")

    def test_save_is_atomic_and_keeps_only_known_keys(self):
        config.save_settings({"theme": "amber", "junk": True})
        path = os.path.join(self.test_dir, "config", "settings.json")
        with open(path) as f:
            payload = json.load(f)
        self.assertEqual(payload["theme"], "amber")
        self.assertNotIn("junk", payload)
        self.assertFalse(os.path.exists(path + ".tmp"))

    def test_corrupt_settings_fall_back_to_defaults(self):
        os.makedirs(os.path.join(self.test_dir, "config"), exist_ok=True)
        with open(os.path.join(self.test_dir, "config", "settings.json"), "w") as f:
            f.write("{not json")
        self.assertEqual(config.load_settings()["theme"], "indigo")


class TestGeometry(ConfigTestCase):
    def test_ui_scale_tracks_height_and_is_clamped(self):
        self.assertAlmostEqual(config.ui_scale_for(320), 1.0, places=3)
        self.assertAlmostEqual(config.ui_scale_for(720), 2.25, places=3)
        self.assertLessEqual(config.ui_scale_for(100), 0.75)
        self.assertLessEqual(config.ui_scale_for(100000), 6.0)

    def test_canvas_is_the_full_display(self):
        """D-029: no letterboxing; the canvas is the display."""
        self.assertEqual(config.canvas_size(1366, 768), (1366, 768))

    def test_design_width_grows_with_resolution(self):
        low = config.design_width(1280, 720)
        high = config.design_width(1920, 1080)
        self.assertGreater(low, 480)
        self.assertAlmostEqual(low, high, places=0)


class TestDisplayEnvironment(ConfigTestCase):
    def test_x11_session_pins_the_x11_driver(self):
        socket_dir = "/tmp/.X11-unix"
        if not os.path.isdir(socket_dir) or not os.access(socket_dir, os.W_OK):
            self.skipTest("no writable X11 socket directory")
        with open(os.path.join(socket_dir, "X77"), "w"):
            pass
        try:
            env = config.prepare_child_environment({"DISPLAY": ":77", "PATH": "/bin"})
            self.assertEqual(env["SDL_VIDEODRIVER"], "x11")
        finally:
            try:
                os.remove("/tmp/.X11-unix/X77")
            except OSError:
                pass

    def test_stale_display_is_dropped(self):
        env = config.prepare_child_environment({"DISPLAY": ":4041", "PATH": "/bin"})
        self.assertNotIn("DISPLAY", env)

    def test_console_pins_the_first_drm_card(self):
        """Two GPUs must not leave SDL on a card with no outputs."""
        fake_cards = ["/dev/dri/card1", "/dev/dri/card0"]
        real_glob = config.glob.glob
        config.glob.glob = lambda pattern: (fake_cards if "card" in pattern
                                            else real_glob(pattern))
        try:
            env = config.prepare_child_environment({"PATH": "/bin"})
            self.assertEqual(env["SDL_VIDEODRIVER"], "kmsdrm")
            self.assertEqual(env["SDL_VIDEO_KMSDRM_DEVICE"], "/dev/dri/card0")
        finally:
            config.glob.glob = real_glob

    def test_other_drm_devices_excludes_the_one_in_use(self):
        real_glob = config.glob.glob
        config.glob.glob = lambda pattern: (["/dev/dri/card0", "/dev/dri/card1"]
                                            if "card" in pattern else real_glob(pattern))
        try:
            self.assertEqual(config.other_drm_devices("/dev/dri/card0"),
                             ["/dev/dri/card1"])
            self.assertEqual(config.other_drm_devices(""), ["/dev/dri/card0",
                                                            "/dev/dri/card1"])
        finally:
            config.glob.glob = real_glob

    def test_headless_uses_dummy_drivers(self):
        if os.environ.get("DISPLAY"):
            self.skipTest("run under X11")
        env = config.prepare_child_environment({"PATH": "/bin"})
        self.assertEqual(env["SDL_VIDEODRIVER"], "dummy")
        self.assertEqual(env["SDL_AUDIODRIVER"], "dummy")


class TestTheme(ConfigTestCase):
    def test_every_theme_exposes_the_full_palette(self):
        for name in theme_module.THEME_ORDER:
            palette = theme_module.Theme(name, 2.0).colors
            for key in ("bg", "panel", "text", "primary", "accent", "highlight",
                        "success", "danger", "info", "cart_body", "cart_label"):
                self.assertTrue(hasattr(palette, key), f"{name}.{key}")

    def test_unknown_theme_falls_back(self):
        self.assertEqual(theme_module.Theme("neon").name, "indigo")

    def test_px_scales_and_never_returns_zero(self):
        th = theme_module.Theme("indigo", 2.0)
        self.assertEqual(th.px(10), 20)
        self.assertGreaterEqual(th.px(0.1), 1)

    def test_page_renders_without_overflow(self):
        surface = pygame.Surface((1280, 720))
        th = theme_module.Theme("violet", 720 / 320)
        th.panel(surface, th.rect(4, 4, 100, 60))
        th.cartridge(surface, th.rect(10, 10, 72, 88),
                     "Pocket Rally Advance", "GBA", "abc12345", selected=True, badge="NEW")
        th.button(surface, th.rect(10, 110, 90, 18), "[A] LAUNCH")
        th.bar(surface, th.rect(10, 132, 90, 6), 0.5, th.colors.success)

    def test_cartridge_fits_tiny_cards(self):
        surface = pygame.Surface((480, 320))
        th = theme_module.Theme("indigo", 1.0)
        for size in (40, 56, 72, 96):
            th.cartridge(surface, pygame.Rect(4, 4, size, int(size * 1.2)),
                         "Super Long Game Title Here", "GB", "deadbeef")


class TestDisplayModeSize(ConfigTestCase):
    """
    Reading the physical display mode from DRM sysfs (never acquiring the
    display itself) so the emulator window can be born screen-sized.
    """

    def make_drm(self, spec):
        root = os.path.join(self.test_dir, "drm")
        for name, (status, modes) in spec.items():
            connector_dir = os.path.join(root, name)
            os.makedirs(connector_dir, exist_ok=True)
            if status is not None:
                with open(os.path.join(connector_dir, "status"), "w") as f:
                    f.write(status + "\n")
            if modes is not None:
                with open(os.path.join(connector_dir, "modes"), "w") as f:
                    f.write("".join(m + "\n" for m in modes))
        return root

    def test_internal_panel_wins_over_external_output(self):
        root = self.make_drm({
            "card0-HDMI-A-1": ("connected", ["3840x2160", "1920x1080"]),
            "card0-eDP-1": ("connected", ["1366x768", "1024x768"]),
        })
        self.assertEqual(config.display_mode_size({}, drm_root=root), (1366, 768))

    def test_first_mode_line_is_the_preferred_mode(self):
        root = self.make_drm({"card0-LVDS-1": ("connected", ["1920x1080", "1366x768"])})
        self.assertEqual(config.display_mode_size({}, drm_root=root), (1920, 1080))

    def test_disconnected_and_missing_connectors_are_skipped(self):
        root = self.make_drm({
            "card0-HDMI-A-1": ("connected", ["1920x1080"]),
            "card0-DP-1": ("disconnected", ["3840x2160"]),
            "card0-VGA-1": (None, None),
        })
        self.assertEqual(config.display_mode_size({}, drm_root=root), (1920, 1080))

    def test_pinned_kmsdrm_card_filters_connectors(self):
        root = self.make_drm({
            "card0-eDP-1": ("connected", ["1366x768"]),
            "card1-DP-1": ("connected", ["1920x1080"]),
        })
        env = {"SDL_VIDEO_KMSDRM_DEVICE": "/dev/dri/card1"}
        self.assertEqual(config.display_mode_size(env, drm_root=root), (1920, 1080))

    def test_garbage_modes_and_absent_sysfs_yield_none(self):
        root = self.make_drm({"card0-eDP-1": ("connected", ["banana", "0x0", "1920x1080i"])})
        self.assertIsNone(config.display_mode_size({}, drm_root=root))
        missing = os.path.join(self.test_dir, "no-such-drm")
        self.assertIsNone(config.display_mode_size({}, drm_root=missing))

    def test_larger_output_wins_among_external_connectors(self):
        root = self.make_drm({
            "card0-HDMI-A-1": ("connected", ["1920x1080"]),
            "card0-DP-1": ("connected", ["3840x2160"]),
        })
        self.assertEqual(config.display_mode_size({}, drm_root=root), (3840, 2160))


if __name__ == "__main__":
    unittest.main()
