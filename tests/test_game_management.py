"""Launcher: favorites, Game Management and in-game shortcut configuration."""

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame  # noqa: E402

import common  # noqa: E402
import config as settings_store  # noqa: E402
import gba_launcher  # noqa: E402
import library  # noqa: E402
from game_data import CheatStore, Favorites, SaveStateStore  # noqa: E402
from gba_launcher import (  # noqa: E402
    GBALauncher, GameManagementOverlay, ShortcutOverlay, StrongConfirmOverlay,
    ConfirmOverlay,
)

GAME_A = "44636205c3e1a1b2"
GAME_B = "80a88361e29f4a77"
GAME_C = "11125a10d4f8b0c1"


class ManagementTestCase(unittest.TestCase):
    def setUp(self):
        pygame.init()
        pygame.joystick.init()
        self.root = tempfile.mkdtemp()
        self.games_dir = os.path.join(self.root, "games")
        os.makedirs(self.games_dir, exist_ok=True)
        self._original = {}

        def patch(module, name, value):
            self._original[(module, name)] = getattr(module, name, None)
            setattr(module, name, value)

        for module in (common, settings_store, gba_launcher, library):
            patch(module, "get_data_dir", lambda: self.root)
            if hasattr(module, "get_games_dir"):
                patch(module, "get_games_dir", lambda: self.games_dir)
        patch(settings_store, "apply_volume", lambda *a, **k: True)
        patch(settings_store, "set_brightness", lambda *a, **k: True)
        patch(gba_launcher, "VolumeWatcher", _FakeWatcher)

    def tearDown(self):
        for (module, name), value in self._original.items():
            setattr(module, name, value)
        shutil.rmtree(self.root, ignore_errors=True)

    def make_launcher(self):
        launcher = GBALauncher(demo=True,
                               screenshot_path=os.path.join(self.root, "shot.png"))
        launcher.screenshot_path = None
        launcher.update()
        launcher.on_tab_activated()
        return launcher

    def rom(self, name):
        path = os.path.join(self.games_dir, name)
        with open(path, "wb") as f:
            f.write(b"ROM DATA " + name.encode())
        return path

    def entries(self, launcher):
        paths = [self.rom("alpha.gba"), self.rom("bravo.gba"), self.rom("charlie.gba")]
        entries = []
        for game_id, title, path in zip((GAME_A, GAME_B, GAME_C),
                                        ("Alpha", "Bravo", "Charlie"), paths):
            entries.append({"id": game_id, "title": title, "filename": os.path.basename(path),
                            "type": "game", "format": "GBA", "content_path": path,
                            "size_bytes": os.path.getsize(path),
                            "handler": "mgba-adapter", "args": []})
        launcher.games = entries
        launcher.selected[0] = 0
        return entries


class _FakeWatcher:
    def __init__(self, *args, **kwargs):
        self.volumes = []

    def start(self):
        pass

    def stop(self):
        pass

    def take_pending(self):
        return []


class TestFavorites(ManagementTestCase):
    def test_favorite_persists_and_always_appears_first(self):
        launcher = self.make_launcher()
        entries = self.entries(launcher)
        self.assertTrue(launcher.toggle_favorite(entries[2]))
        self.assertEqual([g["title"] for g in launcher.visible_games()],
                         ["Charlie", "Alpha", "Bravo"])
        # The selection stays on the same game after the sort.
        self.assertEqual(launcher.current_item()["id"], GAME_A)
        self.assertTrue(Favorites(self.root).is_favorite(GAME_C))

        # A fresh launcher (restart) reads the same persisted favorites.
        restarted = self.make_launcher()
        self.entries(restarted)
        restarted._reorder_games(preserve=False)
        self.assertEqual([g["title"] for g in restarted.visible_games()],
                         ["Charlie", "Alpha", "Bravo"])

    def test_favorites_keep_their_relative_order_without_duplicates(self):
        launcher = self.make_launcher()
        entries = self.entries(launcher)
        launcher.toggle_favorite(entries[2])
        launcher.toggle_favorite(entries[0])
        self.assertEqual([g["title"] for g in launcher.visible_games()],
                         ["Alpha", "Charlie", "Bravo"])
        self.assertEqual(len(launcher.visible_games()), 3)
        launcher.games.append(dict(entries[0]))
        self.assertEqual(len(launcher.visible_games()), 3)

    def test_unfavorite_returns_to_normal_position(self):
        launcher = self.make_launcher()
        entries = self.entries(launcher)
        launcher.toggle_favorite(entries[1])
        self.assertEqual(launcher.visible_games()[0]["title"], "Bravo")
        self.assertFalse(launcher.toggle_favorite(entries[1]))
        self.assertEqual([g["title"] for g in launcher.visible_games()],
                         ["Alpha", "Bravo", "Charlie"])
        self.assertFalse(Favorites(self.root).is_favorite(GAME_B))

    def test_ctrl_f_and_controller_x_toggle_favorite(self):
        launcher = self.make_launcher()
        entries = self.entries(launcher)
        launcher.current_tab = gba_launcher.TAB_GAMES
        launcher.selected[0] = 1
        launcher._handle_key(pygame.event.Event(
            pygame.KEYDOWN, key=pygame.K_f, mod=pygame.KMOD_CTRL))
        self.assertTrue(Favorites(self.root).is_favorite(GAME_B))
        launcher._handle_tab_event(pygame.event.Event(
            pygame.JOYBUTTONDOWN, button=2))
        self.assertFalse(Favorites(self.root).is_favorite(GAME_B))

    def test_favorite_is_visible_in_detail_and_hints(self):
        launcher = self.make_launcher()
        entries = self.entries(launcher)
        launcher.toggle_favorite(entries[0])
        launcher.render()                         # draws grid + detail panel
        hints = launcher.context_hints()
        self.assertTrue(any("favorite" in description for _, description, _ in hints))


class TestGameManagement(ManagementTestCase):
    def test_settings_catalogue_has_a_game_management_section(self):
        launcher = self.make_launcher()
        handlers = [e["handler"] for e in launcher.settings_entries]
        self.assertIn("internal_settings_games", handlers)
        launcher.current_item = lambda: {
            "handler": "internal_settings_games", "type": "settings"}
        launcher.activate_current()
        self.assertEqual(launcher.overlays[-1].title, "GAME MANAGEMENT")

    def test_game_management_page_offers_both_delete_actions(self):
        launcher = self.make_launcher()
        self.entries(launcher)
        launcher.open_settings_page("games")
        overlay = launcher.overlays[-1]
        labels = [row["label"] for row in overlay.rows]
        self.assertIn("Delete Selected Games", labels)
        self.assertIn("Delete All Imported Games", labels)
        overlay.draw(launcher)                    # renders at full resolution
        self.assertEqual(overlay.rows[0]["value"], "3")

    def test_delete_selected_requires_confirmation_and_removes_all_game_data(self):
        launcher = self.make_launcher()
        entries = self.entries(launcher)
        for entry in entries[:2]:
            CheatStore(entry["id"], entry["content_path"],
                       data_dir=self.root).add_cheat("Cheat", "CODE", enabled=True)
            SaveStateStore(entry["id"], data_dir=self.root).save(1, b"STATE", b"PNG")
        Favorites(self.root).set_favorite(GAME_A, True)
        launcher.library.register_content([(e["content_path"], e["id"])
                                           for e in entries])

        manage = GameManagementOverlay(launcher)
        self.assertEqual(len(manage.items), 3)
        manage.handle(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN), launcher)
        manage.handle(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_DOWN), launcher)
        manage.handle(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN), launcher)
        self.assertEqual(len(manage.selected), 2)
        manage.handle(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_d), launcher)
        confirm = launcher.overlays[-1]
        self.assertIsInstance(confirm, ConfirmOverlay)
        # Files stay put until the confirmation is accepted.
        self.assertTrue(os.path.isfile(entries[0]["content_path"]))
        confirm._accept(launcher)
        self.assertFalse(os.path.isfile(entries[0]["content_path"]))
        self.assertFalse(os.path.isfile(entries[1]["content_path"]))
        self.assertTrue(os.path.isfile(entries[2]["content_path"]))
        self.assertFalse(Favorites(self.root).is_favorite(GAME_A))
        self.assertNotIn(entries[0]["content_path"], launcher.library._cache)
        self.assertFalse(os.path.isdir(os.path.join(self.root, "saves", GAME_A)))

    def test_delete_all_requires_a_two_second_hold(self):
        launcher = self.make_launcher()
        entries = self.entries(launcher)
        launcher.confirm_delete_all()
        first = launcher.overlays[-1]
        self.assertIsInstance(first, ConfirmOverlay)
        self.assertTrue(os.path.isfile(entries[0]["content_path"]))
        first._accept(launcher)                   # explicit yes/no gate
        hold = launcher.overlays[-1]
        self.assertIsInstance(hold, StrongConfirmOverlay)
        hold.draw(launcher)                       # no hold -> nothing deleted
        self.assertTrue(os.path.isfile(entries[0]["content_path"]))
        hold.hold_started = time.monotonic() - hold.HOLD_SECONDS - 1
        hold.draw(launcher)                       # hold completed
        self.assertFalse(any(os.path.isfile(e["content_path"]) for e in entries))
        self.assertEqual(launcher._deletable_games(), [])

    def test_delete_all_with_no_games_is_a_no_op(self):
        launcher = self.make_launcher()
        launcher.games = []
        launcher.confirm_delete_all()
        self.assertEqual(launcher.overlays, [])
        self.assertIn("No imported games", launcher.toast_text)


class TestShortcutUI(ManagementTestCase):
    def test_shortcut_overlay_captures_and_persists_a_combo(self):
        launcher = self.make_launcher()
        overlay = ShortcutOverlay(launcher)
        self.assertEqual(overlay.shortcuts["cheats"], ("keyUp", "keyLeft"))
        overlay.handle(pygame.event.Event(pygame.KEYDOWN,
                                          key=pygame.K_RETURN, mod=0), launcher)
        self.assertTrue(overlay.listening)
        # X and Z are the shared profile's A and B buttons.
        overlay.handle(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_x, mod=0),
                       launcher)
        overlay.handle(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_z, mod=0),
                       launcher)
        self.assertEqual(overlay.shortcuts["cheats"], ("keyA", "keyB"))
        self.assertEqual(settings_store.load_shortcuts()["cheats"],
                         ("keyA", "keyB"))
        self.assertEqual(settings_store.load_settings()["shortcut_cheats"],
                         ["keyA", "keyB"])
        overlay.draw(launcher)

    def test_shortcut_reset_restores_defaults(self):
        settings_store.save_shortcuts({"cheats": ["keyA", "keyB"]},
                                      settings_store.load_settings())
        launcher = self.make_launcher()
        overlay = ShortcutOverlay(launcher)
        overlay.handle(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_r, mod=0),
                       launcher)
        self.assertEqual(overlay.shortcuts["cheats"], ("keyUp", "keyLeft"))
        self.assertEqual(settings_store.load_shortcuts()["fast_forward"],
                         ("keyL", "keyR"))

    def test_controls_settings_page_shows_the_three_shortcuts(self):
        launcher = self.make_launcher()
        launcher.open_settings_page("controls")
        overlay = launcher.overlays[-1]
        labels = [row["label"] for row in overlay.rows]
        self.assertIn("In-game shortcuts", labels)
        self.assertIn("Cheats overlay", labels)
        self.assertIn("Fast-forward cycle", labels)
        self.assertIn("Save states overlay", labels)
        overlay.draw(launcher)


if __name__ == "__main__":
    unittest.main()
